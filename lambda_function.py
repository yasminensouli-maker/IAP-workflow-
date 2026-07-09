import json
import base64
import boto3
import time
import os
import re
import secrets
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
ses = boto3.client('ses', region_name='ca-central-1')
# Bedrock/Nova removed entirely — scoring, extraction, and Q&A are now
# deterministic logic in index.html. See scoreFunding(), parseTextDeterministically(),
# answerIntelQuestion(), draftPOPWithNova().

# ── CONFIG (env vars — PRD Section 10; change via console, no code edit) ──
TABLE = os.environ.get('TABLE', 'iap-deals')
BUCKET = os.environ.get('BUCKET', '')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'yasmine@cloudzero.ca')
APP_URL = os.environ.get('APP_URL', 'https://main.dgxv59n7ru973.amplifyapp.com')

RATE_MIGRATE = float(os.environ.get('RATE_MIGRATE', '0.045'))      # Migrate / Modernize
RATE_OPTIMIZE = float(os.environ.get('RATE_OPTIMIZE', '0.01'))
OPTIMIZE_CAP = float(os.environ.get('OPTIMIZE_CAP', '250000'))
BLENDED_DISCOUNT = float(os.environ.get('BLENDED_DISCOUNT', '0.20'))
REVIEW_REMINDER_DAYS = int(os.environ.get('REVIEW_REMINDER_DAYS', '5'))
MILESTONE_LEAD_DAYS = int(os.environ.get('MILESTONE_LEAD_DAYS', '30'))

# Approver emails — comma-separated env vars. CHRIS_EMAIL empty until provided.
REVIEWER_EMAILS = [e.strip() for e in os.environ.get(
    'REVIEWER_EMAILS', 'yasmine@cloudzero.ca,reidelj@amazon.com').split(',') if e.strip()]
CHRIS_EMAIL = os.environ.get('CHRIS_EMAIL', '').strip()
if CHRIS_EMAIL and CHRIS_EMAIL not in REVIEWER_EMAILS:
    REVIEWER_EMAILS.append(CHRIS_EMAIL)
INTEL_EMAILS = [e.strip() for e in os.environ.get(
    'INTEL_EMAILS', 'akanksha.r.bilani@intel.com,brendon.roosken@intel.com,deep.grewal@intel.com').split(',') if e.strip()]
TCC_EMAIL = os.environ.get('TCC_EMAIL', 'jacobx.barksdale@intel.com').strip()
ELIGIBLE_FAMILIES = [f.strip() for f in os.environ.get(
    'ELIGIBLE_FAMILIES', 'm8i,c8i,r8i,x8i').split(',') if f.strip()]

# ── ADMIN & APPROVER LOGINS — fixed named list, lives here only, never sent
# to the browser. Override any password via Lambda console env vars (e.g.
# ADMIN_PASS_YASMINE) without a code change or redeploy of secrets in git. ──
def _admin_pass(env_key, default):
    return os.environ.get(env_key, default)

ADMIN_USERS = {
    'yasmine@cloudzero.ca':        {'pass': _admin_pass('ADMIN_PASS_YASMINE','CZ@dmin1'),  'tier':'admin', 'name':'Yasmine',        'label':'CloudZero Admin', 'approver':'core'},
    'reidelj@amazon.com':          {'pass': _admin_pass('ADMIN_PASS_JEANINE','Core2026'),  'tier':'core',  'name':'Jeanine Reidel', 'label':'AWS Approval',    'approver':'core'},
    'akanksha.r.bilani@intel.com': {'pass': _admin_pass('ADMIN_PASS_AKANKSHA','Intel2026'),'tier':'intel_approver','name':'Akanksha Bilani','label':'Intel Leadership','approver':'intel'},
    'brendon.roosken@intel.com':   {'pass': _admin_pass('ADMIN_PASS_BRENDON','Intel2026'), 'tier':'intel_approver','name':'Brendon Roosken','label':'Intel Leadership','approver':'intel'},
    'deep.grewal@intel.com':       {'pass': _admin_pass('ADMIN_PASS_DEEP','Intel2026'),    'tier':'intel_approver','name':'Deep Grewal',    'label':'Intel Leadership','approver':'intel'},
    'jacobx.barksdale@intel.com':  {'pass': _admin_pass('ADMIN_PASS_TCC','TCC2026'),       'tier':'tcc',   'name':'Jacob Barksdale','label':'TCC',             'approver':'tcc'},
}

# People who log in through the open @amazon.com / @intel.com domain buttons
# but need approver-level access rather than the generic seller tier —
# checked by email after the shared domain password succeeds.
DOMAIN_APPROVER_UPGRADES = {
    'deep.grewal@intel.com':      {'tier':'intel_approver','name':'Deep Grewal',    'label':'Intel Leadership','approver':'intel'},
    'jacobx.barksdale@intel.com': {'tier':'tcc',            'name':'Jacob Barksdale','label':'TCC',             'approver':'tcc'},
}

# PRD Section 7 statuses. Old stage values map forward for existing records.
STATUS_MAP_OLD_TO_NEW = {
    'core': 'Under Review',
    'intel': 'Approved (DNE Set)',
    'tcc': 'Intel Leadership Approved',
    'approved': 'SOW Issued',
    'changes_requested': 'Under Review',
}

def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def send_email(to_addresses, subject, body_text):
    try:
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={'ToAddresses': to_addresses},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body_text, 'Charset': 'UTF-8'}}
            }
        )
        return True
    except Exception as e:
        print(f"SES error to {to_addresses}: {str(e)}")
        return False

def log_email(deal, recipients, subject):
    """PRD Section 8: all emails tied to the deal record for audit."""
    deal.setdefault('emailLog', []).append({
        'at': now_utc(), 'to': recipients, 'subject': subject
    })

def compute_dne(target_arr, deal_type, program='standard'):
    """DNE = ARR x (1 - blended discount) x rate.
    Migrate: 4.5%, uncapped. Modernize: 1%, capped $250K.
    Same rate table for both Standard and Program 2 (Modernization) —
    program only changes which governance/payment rules apply."""
    arr = float(target_arr or 0)
    eligible = arr * (1 - BLENDED_DISCOUNT)
    dt = str(deal_type or '').lower()
    if dt.startswith('mod'):
        return min(eligible * RATE_OPTIMIZE, OPTIMIZE_CAP)
    return eligible * RATE_MIGRATE

def deal_summary_block(deal):
    dne = float(deal.get('dne', 0) or 0)
    return f"""Deal details:
- Deal name: {deal.get('dealName', '')}
- Customer: {deal.get('custName', '')}
- Partner: {deal.get('partnerName', '')}
- Deal type: {deal.get('actType', '')}
- ACE Opportunity ID: {deal.get('aceID', 'Pending')}
- ACE Amount: ${float(deal.get('aceAmount', 0) or 0):,.2f}
- Payment option: {deal.get('paymentOption', 'Quarterly')}
- Migration target date: {deal.get('migTargetDate', deal.get('migStart', 'TBD'))}
- DNE: ${dne:,.2f}
- Win Wire: {'Yes' if deal.get('winWire') else 'No'}
- Status: {deal.get('status', '')}

Review in the app: {APP_URL}"""

# ── SES TRIGGERS (PRD Section 8) ──
def notify_submitted(deal):
    subject = f"IAP Deal Submitted: {deal.get('custName', 'New Deal')}"
    body = f"""A new deal has been submitted to the Intel Accelerate Program and is pending internal review.

{deal_summary_block(deal)}

Next step: review the deal, run the DNE calculator, and approve to route to Intel leadership.
{time.strftime('%B %d, %Y')}"""
    ok_sent = send_email(REVIEWER_EMAILS, subject, body)
    if ok_sent:
        log_email(deal, REVIEWER_EMAILS, subject)
    return ok_sent

def notify_intel(deal):
    subject = f"IAP Deal Pending Intel Approval: {deal.get('custName', 'New Deal')} — DNE ${float(deal.get('dne',0) or 0):,.0f}"
    body = f"""A deal has cleared internal review. The DNE is set. One approval from Intel leadership is required.

{deal_summary_block(deal)}

Reply through the app: approve, or ask a question. Questions are logged against the deal record.
{time.strftime('%B %d, %Y')}"""
    ok_sent = send_email(INTEL_EMAILS, subject, body)
    if ok_sent:
        log_email(deal, INTEL_EMAILS, subject)
    return ok_sent

def notify_question(deal, question, asked_by):
    subject = f"IAP Question from Intel Leadership: {deal.get('custName', '')}"
    body = f"""{asked_by} asked a question on this deal:

"{question}"

{deal_summary_block(deal)}"""
    recips = REVIEWER_EMAILS
    ok_sent = send_email(recips, subject, body)
    if ok_sent:
        log_email(deal, recips, subject)
    return ok_sent

def notify_intel_approved(deal):
    subject = f"IAP Intel Leadership Approved: {deal.get('custName', '')} — Ready for SOW"
    recips = list(dict.fromkeys(REVIEWER_EMAILS + [TCC_EMAIL]))
    body = f"""Intel leadership has approved this deal. TCC can generate the SOW.

{deal_summary_block(deal)}

Next steps: TCC amends and issues the SOW. Proof of Performance items, including Cost Explorer, are collected after SOW signing.
{time.strftime('%B %d, %Y')}"""
    ok_sent = send_email(recips, subject, body)
    if ok_sent:
        log_email(deal, recips, subject)
    return ok_sent

def notify_sow_issued(deal):
    subject = f"IAP SOW Issued: {deal.get('custName', '')}"
    team = deal.get('team', [{}])
    submitter_email = team[0].get('email', '') if team else ''
    recips = list(dict.fromkeys(REVIEWER_EMAILS + ([submitter_email] if submitter_email else [])))
    body = f"""The SOW has been issued for this deal. Post-SOW execution moves to Smartsheet tracking.

{deal_summary_block(deal)}"""
    ok_sent = send_email(recips, subject, body)
    if ok_sent:
        log_email(deal, recips, subject)
    return ok_sent

def audit(deal, editor, field, old, new):
    """PRD Section 6: who, field, old -> new, UTC timestamp."""
    if str(old) == str(new):
        return
    deal.setdefault('auditLog', []).append({
        'at': now_utc(), 'by': editor or 'unknown',
        'field': field, 'old': '' if old is None else str(old), 'new': '' if new is None else str(new)
    })

AUDITED_FIELDS = ['status', 'dne', 'migTargetDate', 'migStart', 'closeDate', 'aceAmount',
                  'aceID', 'paymentOption', 'winWire', 'targetArr', 'actType', 'custName',
                  'partnerName', 'dealName']

# ── SMARTSHEET (token lives in Secrets Manager, never in code or chat) ──
SMARTSHEET_SECRET = os.environ.get('SMARTSHEET_SECRET', 'iap/smartsheet-token')
SMARTSHEET_SHEET_ID = os.environ.get('SMARTSHEET_SHEET_ID', '')
_ss_token_cache = {'token': None}

def get_smartsheet_token():
    if _ss_token_cache['token']:
        return _ss_token_cache['token']
    try:
        sm = boto3.client('secretsmanager', region_name='ca-central-1')
        val = sm.get_secret_value(SecretId=SMARTSHEET_SECRET)
        _ss_token_cache['token'] = json.loads(val['SecretString']).get('token')
        return _ss_token_cache['token']
    except Exception as e:
        print(f"Smartsheet token unavailable: {str(e)}")
        return None

def push_to_smartsheet(deal):
    """Add the deal as a row on the IAP Project Intake Sheet, mapped to the
    full confirmed column list. TCC/admin-only columns (assigned after
    submission — IPIC #, POP dates, Claim Quarter, Intel Budget Year,
    Contribution/Claimed/Paid/Remaining amounts, SharePoint link) are left
    blank so TCC fills them directly in Smartsheet. Column matching is
    tolerant by title — only columns that actually exist get written to."""
    token = get_smartsheet_token()
    if not token:
        return 'Not synced — token not configured in Secrets Manager'
    if not SMARTSHEET_SHEET_ID:
        return 'Not synced — SMARTSHEET_SHEET_ID env var not set'
    import urllib.request as _ur
    base = f"https://api.smartsheet.com/2.0/sheets/{SMARTSHEET_SHEET_ID}"
    hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        req = _ur.Request(base + '?pageSize=1', headers=hdrs)
        with _ur.urlopen(req, timeout=15) as r:
            sheet = json.loads(r.read().decode())
        cols = {c['title']: c['id'] for c in sheet.get('columns', [])}

        team = deal.get('team', [{}])
        submitter = team[0] if team else {}
        service_arr = (
            float(deal.get('arrEc2', 0) or 0) + float(deal.get('arrRds', 0) or 0) +
            float(deal.get('arrElastiCache', 0) or 0) + float(deal.get('arrOpenSearch', 0) or 0)
        )
        infra_arr = service_arr or float(deal.get('targetArr', 0) or 0)
        dne = float(deal.get('dne', 0) or 0)
        migration_cost = float(deal.get('migrationCost', 0) or 0)
        act_type = str(deal.get('actType', '') or '')
        is_migrate = act_type.lower().startswith('migrat')
        activity_type_label = 'Migration' if is_migrate else ('Modernization' if act_type else '')
        funding_pct = '1% (capped $250,000)' if not is_migrate and act_type else ('4.5%' if is_migrate else '')
        is_migration_yn = 'Yes' if is_migrate else ('No' if act_type else '')
        pop_due = ''
        try:
            end_d = datetime.strptime(deal.get('closeDate', ''), '%Y-%m-%d')
            pop_due = (end_d.replace(day=min(end_d.day, 28))).strftime('%Y-%m-%d')
        except Exception:
            pass
        expected_roi = ''
        if migration_cost > 0 and dne > 0:
            expected_roi = f"{round((dne - migration_cost) / migration_cost * 100)}%"
        activity_desc = f"{act_type or 'Deal'} for {deal.get('custName','')} — {deal.get('workload','') or 'workload not specified'}".strip(' —')

        # Submitter-derivable fields — written now.
        # Blank string values are dropped before sending, so any field genuinely
        # unknown at submission (marked TCC-only below) simply stays untouched.
        candidates = {
            # Activity tracking — submitter provides where marked, rest is TCC/admin post-SOW
            'IPIC Activity #': deal.get('ipicNum', ''),                          # TCC-only
            'Activity Name': deal.get('dealName', ''),
            'Activity Type': activity_type_label,
            'IPIC Activity Description': activity_desc,
            'Start Date': deal.get('migStart', ''),
            'End Date': deal.get('closeDate', '') or deal.get('migTargetDate', ''),
            'POP Due Date': pop_due,                                            # estimate; TCC confirms
            'Claim Quarter': deal.get('claimQuarter', ''),                      # TCC-only
            'Partner or End Customer Name': deal.get('partnerName', '') or deal.get('custName', ''),
            'Status': deal.get('status', ''),
            'Notes': deal.get('notes', ''),
            'POP Received Date': '',                                           # TCC-only
            'Claim Submitted Date': '',                                        # TCC-only
            'IPIC Activity Creation Date': (deal.get('submittedAt', '') or '')[:10],
            'Intel Budget Year': deal.get('intelBudgetYear', ''),
            'Intel Contribution Amount': dne,
            'ACE Opportunity ID': deal.get('aceID', ''),
            'Funding amount not to exceed': dne,
            'Funding Percentage': funding_pct,
            'Amount Claimed': '',                                              # TCC-only
            'Amount Paid': '',                                                 # TCC-only
            'Amount Remaining': '',                                            # TCC-managed running total
            'AWS Alignment': deal.get('awsRegion', ''),
            'Link to SharePoint': '',                                          # added later by admin/TCC

            # Intake form entries
            'Intake Entry Date': (deal.get('submittedAt', '') or '')[:10],
            'Submitter Name': submitter.get('name', ''),
            'Submitter Email': submitter.get('email', ''),
            'Intel Rep Name': deal.get('intelRepName', ''),
            'AWS Rep Name': deal.get('awsRepName', ''),
            'Migration Project Description': deal.get('workload', ''),
            'POP Available?': 'No',                                           # true only post-SOW
            'AWS Instances Used': deal.get('migTo', ''),
            'Region/Country of Execution': deal.get('awsRegion', ''),
            'Cost of Infrastructure (ARR)': infra_arr,
            'Cost of Migration (Engineering Work)': migration_cost,
            'Workload Selection': ', '.join(deal.get('workloadSelection', []) or []),
            'Requested funding amount': dne,
            'Is this for a Migration activity?': is_migration_yn,
            'Project Description (Other)': '',
            'Project Description (Event)': '',
            'Add link to AWS Pricing Calculator': '',                         # field removed from submitter form
            'Link to Pricing Calculator': '',                                 # field removed from submitter form
            'Is this a migration activity?': is_migration_yn,
            'Activity Description': activity_desc,
            'Expected ROI': expected_roi,
        }
        cells = [{'columnId': cols[k], 'value': v} for k, v in candidates.items()
                 if k in cols and v not in ('', None, 0)]
        if not cells:
            return 'Not synced — sheet columns do not match expected titles'
        payload = json.dumps({'toBottom': True, 'cells': cells}).encode()
        req = _ur.Request(base + '/rows', data=payload, headers=hdrs, method='POST')
        with _ur.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
        row_id = (resp.get('result') or {}).get('id', '')
        return f'Synced to Smartsheet — row {row_id}'
    except Exception as e:
        print(f"Smartsheet push failed: {str(e)}")
        return f'Sync failed — {str(e)[:120]}'

def lambda_handler(event, context):
    # CORS locked to this app's actual domain(s) — no more wildcard '*'.
    # Add a custom domain via ALLOWED_ORIGINS env var (comma-separated) if one
    # ever gets set up in front of the Amplify URL.
    ALLOWED_ORIGINS = [APP_URL] + [
        o.strip() for o in os.environ.get('ALLOWED_ORIGINS', '').split(',') if o.strip()
    ]
    request_origin = event.get('headers', {}).get('origin', '') or event.get('headers', {}).get('Origin', '')
    allow_origin = request_origin if request_origin in ALLOWED_ORIGINS else APP_URL
    headers = {
        'Access-Control-Allow-Origin': allow_origin,
        'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json',
        'Vary': 'Origin'
    }
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    if method == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': '{}'}

    try:
        path = event.get('rawPath', '/')
        body = {}
        if event.get('body'):
            raw = event['body']
            if event.get('isBase64Encoded'):
                raw = base64.b64decode(raw).decode('utf-8')
            body = json.loads(raw)
        table = dynamodb.Table(TABLE)

        # ── SAVE DEAL (with audit diff + status-driven emails) ──
        if path == '/deal' and method == 'POST':
            deal = body.get('deal', {})
            editor = body.get('editor', '')
            if not deal.get('id'):
                deal['id'] = str(int(time.time() * 1000))
            deal['updatedAt'] = int(time.time())

            # Migrate any legacy stage value forward
            if deal.get('approvalStage') in STATUS_MAP_OLD_TO_NEW and not body.get('statusExplicit'):
                deal['status'] = deal.get('status') or STATUS_MAP_OLD_TO_NEW[deal['approvalStage']]

            # Diff against existing record for the audit trail
            old_item = {}
            try:
                old_item = table.get_item(Key={'id': deal['id']}).get('Item', {}) or {}
            except Exception:
                pass
            if old_item:
                deal.setdefault('auditLog', old_item.get('auditLog', []))
                deal.setdefault('emailLog', old_item.get('emailLog', []))
                deal.setdefault('qaLog', old_item.get('qaLog', []))
                deal.setdefault('sowVersions', old_item.get('sowVersions', []))
                for f in AUDITED_FIELDS:
                    if f in deal:
                        audit(deal, editor, f, old_item.get(f), deal.get(f))

            prev_status = old_item.get('status', '')
            curr_status = deal.get('status', '')

            # Status-transition emails (PRD Section 8)
            if body.get('submitted') and not old_item:
                deal['status'] = curr_status = 'Submitted'
                deal['submittedAt'] = now_utc()
                deal['stageEnteredAt'] = deal['submittedAt']
                if not notify_submitted(deal):
                    deal.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': 'Submitted', 'note': 'Submit notification failed to send — check SES verification for recipients.'})
                deal['smartsheetSync'] = push_to_smartsheet(deal)
            elif prev_status != curr_status:
                deal['stageEnteredAt'] = now_utc()
                if curr_status == 'Approved (DNE Set)':
                    if not notify_intel(deal):
                        deal.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': curr_status, 'note': 'Intel Leadership notification failed — check SES verification for Intel recipients.'})
                elif curr_status == 'Intel Leadership Approved':
                    if not notify_intel_approved(deal):
                        deal.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': curr_status, 'note': 'TCC notification failed — check SES verification.'})
                elif curr_status == 'SOW Issued':
                    if not notify_sow_issued(deal):
                        deal.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': curr_status, 'note': 'SOW-issued notification failed — check SES verification.'})

            table.put_item(Item=json.loads(json.dumps(deal), parse_float=str))
            return ok(headers, {'saved': True, 'id': deal['id'], 'status': deal.get('status', '')})

        # ── LIST DEALS ──
        if path == '/deals' and method == 'GET':
            resp = table.scan()
            items = [d for d in resp.get('Items', []) if not str(d.get('id', '')).startswith('config#')]
            return ok(headers, {'deals': items})

        # ── LOGIN LOG — who signed in, when, how many times. Admin-facing. ──
        if path == '/auth/login-log' and method == 'GET':
            resp = table.scan()
            items = [d for d in resp.get('Items', []) if str(d.get('id', '')).startswith('LOGIN#')]
            items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            return ok(headers, {'logins': items})

        # ── DELETE A DEAL — permanent, no undo. Logged to CloudWatch for an audit
        # trail since there's no server-side session token yet to verify tier;
        # the frontend only shows this to admins, but that's a UI gate, not a
        # security boundary. Flagged as a known gap, not treated as fixed.
        if path == '/deals/delete' and method == 'POST':
            deal_id = body.get('id')
            deleted_by = body.get('deletedBy', 'unknown')
            if not deal_id:
                return {'statusCode': 400, 'headers': headers, 'body': json.dumps({'error': 'Missing deal id.'})}
            existing = table.get_item(Key={'id': deal_id}).get('Item')
            print(f"[DELETE DEAL] id={deal_id} custName={(existing or {}).get('custName')} deletedBy={deleted_by} existed={existing is not None}")
            table.delete_item(Key={'id': deal_id})
            return ok(headers, {'deleted': True, 'id': deal_id})

        # ── DNE CALC (server-side source of truth) ──
        if path == '/dne' and method == 'POST':
            dne = compute_dne(body.get('targetArr', 0), body.get('dealType', 'Migrate'), body.get('program', 'standard'))
            return ok(headers, {'dne': round(dne, 2), 'blendedDiscount': BLENDED_DISCOUNT,
                                'rateMigrate': RATE_MIGRATE, 'rateOptimize': RATE_OPTIMIZE,
                                'optimizeCap': OPTIMIZE_CAP})

        # ── Q&A LOG (PRD Stage 3) ──
        if path == '/question' and method == 'POST':
            deal_id = body.get('dealId', '')
            question = body.get('question', '')
            asked_by = body.get('askedBy', '')
            item = table.get_item(Key={'id': deal_id}).get('Item')
            if not item:
                return ok(headers, {'error': 'deal not found'})
            item.setdefault('qaLog', []).append({
                'at': now_utc(), 'type': 'question', 'by': asked_by, 'text': question})
            notify_question(item, question, asked_by)
            table.put_item(Item=json.loads(json.dumps(item), parse_float=str))
            return ok(headers, {'logged': True})

        if path == '/answer' and method == 'POST':
            deal_id = body.get('dealId', '')
            answer = body.get('answer', '')
            by = body.get('by', '')
            item = table.get_item(Key={'id': deal_id}).get('Item')
            if not item:
                return ok(headers, {'error': 'deal not found'})
            item.setdefault('qaLog', []).append({
                'at': now_utc(), 'type': 'answer', 'by': by, 'text': answer})
            subject = f"IAP Question Answered: {item.get('custName', '')}"
            body_txt = f"""{by} answered:

"{answer}"

{deal_summary_block(item)}"""
            if send_email(INTEL_EMAILS, subject, body_txt):
                log_email(item, INTEL_EMAILS, subject)
            table.put_item(Item=json.loads(json.dumps(item), parse_float=str))
            return ok(headers, {'logged': True})

        # ── SOW VERSION (PRD Stage 4) ──
        if path == '/sow-version' and method == 'POST':
            deal_id = body.get('dealId', '')
            requested_by = body.get('requestedBy', '')
            content = body.get('content', '')
            item = table.get_item(Key={'id': deal_id}).get('Item')
            if not item:
                return ok(headers, {'error': 'deal not found'})
            versions = item.setdefault('sowVersions', [])
            versions.append({'version': len(versions) + 1, 'at': now_utc(),
                             'requestedBy': requested_by, 'content': content})
            table.put_item(Item=json.loads(json.dumps(item), parse_float=str))
            return ok(headers, {'saved': True, 'version': len(versions)})

        # ── REMINDERS (EventBridge daily — PRD Section 8 rows 5-7) ──
        if path == '/reminders' and method in ('GET', 'POST'):
            sent = []
            resp = table.scan()
            now_ts = time.time()
            for d in resp.get('Items', []):
                if str(d.get('id', '')).startswith('config#'):
                    continue
                status = d.get('status', '')
                # Stuck in Under Review > threshold (business days approximated as calendar x 1.4)
                if status in ('Submitted', 'Under Review'):
                    sub_at = d.get('submittedAt', '')
                    try:
                        sub_ts = datetime.strptime(sub_at, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
                    except Exception:
                        sub_ts = float(d.get('updatedAt', now_ts))
                    if (now_ts - sub_ts) > REVIEW_REMINDER_DAYS * 1.4 * 86400 and not d.get('stuckReminderSent'):
                        subject = f"IAP Reminder: {d.get('custName','Deal')} pending review {REVIEW_REMINDER_DAYS}+ business days"
                        if send_email(REVIEWER_EMAILS, subject, deal_summary_block(d)):
                            d['stuckReminderSent'] = True
                            log_email(d, REVIEWER_EMAILS, subject)
                            table.put_item(Item=json.loads(json.dumps(d), parse_float=str))
                            sent.append(d.get('id'))
                # Migration date approaching
                mig = d.get('migTargetDate', '') or d.get('migStart', '')
                if mig and status not in ('Complete',):
                    try:
                        mig_ts = datetime.strptime(mig, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
                        days_out = (mig_ts - now_ts) / 86400
                        if 0 < days_out <= MILESTONE_LEAD_DAYS and not d.get('migReminderSent'):
                            subject = f"IAP Migration Date Approaching: {d.get('custName','Deal')} — {mig}"
                            recips = list(dict.fromkeys([TCC_EMAIL] + REVIEWER_EMAILS))
                            if send_email(recips, subject, deal_summary_block(d)):
                                d['migReminderSent'] = True
                                log_email(d, recips, subject)
                                table.put_item(Item=json.loads(json.dumps(d), parse_float=str))
                                sent.append(d.get('id'))
                    except Exception:
                        pass
            return ok(headers, {'remindersSent': sent})

        # ── CONFIG (kept from v1) ──
        if path == '/config' and method == 'POST':
            key = body.get('key', '')
            value = body.get('value', '')
            table.put_item(Item={'id': 'config#' + key, 'value': str(value), 'updatedAt': int(time.time())})
            return ok(headers, {'saved': True, 'key': key})
        if path == '/config' and method == 'GET':
            key = event.get('queryStringParameters', {}).get('key', '') if event.get('queryStringParameters') else ''
            resp = table.get_item(Key={'id': 'config#' + key})
            return ok(headers, {'key': key, 'value': resp.get('Item', {}).get('value')})

        # ── UPLOAD ATTACHMENT (Simple Monthly Calculator at submission; CE post-SOW) ──
        if path == '/upload' and method == 'POST':
            filename = body.get('filename', 'file')
            filedata = body.get('data', '')
            deal_id = body.get('dealId', 'unassigned')
            kind = body.get('kind', 'attachment')
            key = f"{kind}/{deal_id}/{int(time.time())}-{filename}"
            s3.put_object(Bucket=BUCKET, Key=key, Body=base64.b64decode(filedata),
                          ServerSideEncryption='AES256')
            return ok(headers, {'uploaded': True, 'key': key})

        # AI-based extraction, scoring, drafting, and Q&A were removed —
        # replaced with deterministic logic entirely in the frontend.
        # See index.html: scoreFunding(), parseTextDeterministically(),
        # answerIntelQuestion(), and draftPOPWithNova().
        # ── INTEL PRICING PROXY (kept) ──
        if path == '/intel/price' and method == 'POST':
            import urllib.request as _ur
            message = body.get('message', '') or body.get('question', '')
            if not message:
                return ok(headers, {'error': 'no message'})
            req = _ur.Request(
                'http://52.26.245.170:8502/api/chat',
                data=json.dumps({'message': message}).encode(),
                headers={'Content-Type': 'application/json',
                         'X-API-Key': 'intel-arch-7f3a9c2e8b14d05f6a1e9d7c3b8f240a'},
                method='POST')
            with _ur.urlopen(req, timeout=15) as intel_resp:
                return ok(headers, json.loads(intel_resp.read().decode()))

        def notify_login(email, tier, label, via):
            send_email([FROM_EMAIL], f'IAP Deal Desk sign-in — {email}',
                       f'{email} signed in just now.\n\nTier: {tier} ({label})\nMethod: {via}\nTime (UTC): {now_utc()}')
            # Also store as a queryable record — the email tells you in the
            # moment, this is what lets you look back and count/list later.
            try:
                table.put_item(Item={
                    'id': 'LOGIN#' + str(int(time.time()*1000)) + '#' + email,
                    'email': email, 'tier': tier, 'label': label,
                    'method': via, 'timestamp': now_utc()
                })
            except Exception as e:
                print(f"[LOGIN LOG ERROR] failed to store login record: {e}")

        # ── AUTH: ADMIN & APPROVER LOGIN (fixed named list, server-side only) ──
        # These are the people who don't rotate: CloudZero (Yasmine, Hisham),
        # AWS Approval (Jeanine), Intel Leadership (Akanksha, Brendon, Deep), TCC (Jacob).
        # Passwords live here, in the backend, never shipped to the browser.
        # Override any password via Lambda env vars without touching code.
        if path == '/auth/admin-login' and method == 'POST':
            email = (body.get('email') or '').strip().lower()
            password = (body.get('password') or '').strip()
            admin = ADMIN_USERS.get(email)
            print(f"[LOGIN ATTEMPT] email received: '{email}' | email recognized: {admin is not None} | password length received: {len(password)}")
            if not admin:
                print(f"[LOGIN FAIL] '{email}' is not a key in ADMIN_USERS. Known keys: {list(ADMIN_USERS.keys())}")
                return {'statusCode': 401, 'headers': headers, 'body': json.dumps({'error': 'Incorrect email or password.'})}
            if admin['pass'] != password:
                print(f"[LOGIN FAIL] email matched '{email}' but password did not match stored value (lengths: received={len(password)}, stored={len(admin['pass'])})")
                return {'statusCode': 401, 'headers': headers, 'body': json.dumps({'error': 'Incorrect email or password.'})}
            print(f"[LOGIN OK] '{email}' authenticated successfully as tier={admin['tier']}")
            notify_login(email, admin['tier'], admin['label'], 'password')
            return ok(headers, {
                'email': email, 'name': admin['name'], 'tier': admin['tier'],
                'label': admin['label'], 'approver': admin.get('approver'),
                'partnerFilter': admin.get('partnerFilter')
            })

        # ── AUTH: DOMAIN LOGIN — any real @amazon.com or @intel.com email,
        # one shared password. No pre-provisioned account needed. Deep and
        # Jacob get bumped to approver tier automatically by email even
        # though they're using the shared password like everyone else.
        DOMAIN_PASSWORD = os.environ.get('DOMAIN_PASSWORD', 'IAP@2026')
        if path == '/auth/domain-login' and method == 'POST':
            email = (body.get('email') or '').strip().lower()
            password = (body.get('password') or '').strip()
            which = (body.get('domain') or '').strip().lower()  # 'aws' or 'intel'
            expected_domain = 'amazon.com' if which == 'aws' else 'intel.com'
            actual_domain = email.split('@')[-1] if '@' in email else ''
            print(f"[DOMAIN LOGIN ATTEMPT] email='{email}' expected_domain='{expected_domain}' actual_domain='{actual_domain}'")
            if actual_domain != expected_domain:
                return {'statusCode': 403, 'headers': headers,
                        'body': json.dumps({'error': f'Use a real @{expected_domain} email address.'})}
            if password != DOMAIN_PASSWORD:
                print(f"[DOMAIN LOGIN FAIL] '{email}' password did not match. Received length={len(password)}, expected length={len(DOMAIN_PASSWORD)}")
                return {'statusCode': 401, 'headers': headers, 'body': json.dumps({'error': 'Incorrect password.'})}
            upgrade = DOMAIN_APPROVER_UPGRADES.get(email)
            tier = upgrade['tier'] if upgrade else ('aws' if which == 'aws' else 'intel')
            name = upgrade['name'] if upgrade else email.split('@')[0]
            label = upgrade['label'] if upgrade else ('AWS Field' if which == 'aws' else 'Intel Field')
            approver = upgrade.get('approver') if upgrade else None
            print(f"[DOMAIN LOGIN OK] '{email}' as tier={tier}")
            notify_login(email, tier, label, 'domain password')
            return ok(headers, {'email': email, 'name': name, 'tier': tier, 'label': label, 'approver': approver})

        return {'statusCode': 404, 'headers': headers, 'body': json.dumps({'error': 'not found'})}

    except Exception as e:
        print(f"Handler error: {str(e)}")
        return {'statusCode': 500, 'headers': headers, 'body': json.dumps({'error': str(e)})}

def ok(headers, data):
    return {'statusCode': 200, 'headers': headers, 'body': json.dumps(data, default=str)}
