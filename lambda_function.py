import json
import base64
import boto3
import time
import os
import re
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
ses = boto3.client('ses', region_name='ca-central-1')
bedrock = boto3.client('bedrock-runtime', region_name='ca-central-1')
# Document extraction runs via us-east-1: Nova cross-region inference profiles
# (us.*) are not resolvable from ca-central-1, which breaks Converse there.
bedrock_us = boto3.client('bedrock-runtime', region_name='us-east-1')

# ── CONFIG (env vars — PRD Section 10; change via console, no code edit) ──
TABLE = os.environ.get('TABLE', 'iap-deals')
BUCKET = os.environ.get('BUCKET', '')
NOVA_MODEL = os.environ.get('NOVA_MODEL', 'amazon.nova-2-lite-v1:0')
NOVA_EXTRACT_MODEL = os.environ.get('NOVA_EXTRACT_MODEL', 'us.amazon.nova-2-lite-v1:0')
NOVA_EXTRACT_FALLBACK = os.environ.get('NOVA_EXTRACT_FALLBACK', 'us.amazon.nova-lite-v1:0')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'yasmine@cloudzero.ca')
APP_URL = os.environ.get('APP_URL', 'https://main.dgxv59n7ru973.amplifyapp.com')

RATE_MIGRATE = float(os.environ.get('RATE_MIGRATE', '0.045'))      # Migrate / Modernize
RATE_OPTIMIZE = float(os.environ.get('RATE_OPTIMIZE', '0.01'))
OPTIMIZE_CAP = float(os.environ.get('OPTIMIZE_CAP', '250000'))
BLENDED_DISCOUNT = float(os.environ.get('BLENDED_DISCOUNT', '0.30'))
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

def compute_dne(target_arr, deal_type):
    """PRD Stage 2: DNE = ARR x (1 - blended discount) x rate. Optimize capped."""
    arr = float(target_arr or 0)
    eligible = arr * (1 - BLENDED_DISCOUNT)
    if str(deal_type).lower().startswith('opt'):
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
    if send_email(REVIEWER_EMAILS, subject, body):
        log_email(deal, REVIEWER_EMAILS, subject)

def notify_intel(deal):
    subject = f"IAP Deal Pending Intel Approval: {deal.get('custName', 'New Deal')} — DNE ${float(deal.get('dne',0) or 0):,.0f}"
    body = f"""A deal has cleared internal review. The DNE is set. One approval from Intel leadership is required.

{deal_summary_block(deal)}

Reply through the app: approve, or ask a question. Questions are logged against the deal record.
{time.strftime('%B %d, %Y')}"""
    if send_email(INTEL_EMAILS, subject, body):
        log_email(deal, INTEL_EMAILS, subject)

def notify_question(deal, question, asked_by):
    subject = f"IAP Question from Intel Leadership: {deal.get('custName', '')}"
    body = f"""{asked_by} asked a question on this deal:

"{question}"

{deal_summary_block(deal)}"""
    recips = REVIEWER_EMAILS
    if send_email(recips, subject, body):
        log_email(deal, recips, subject)

def notify_intel_approved(deal):
    subject = f"IAP Intel Leadership Approved: {deal.get('custName', '')} — Ready for SOW"
    recips = list(dict.fromkeys(REVIEWER_EMAILS + [TCC_EMAIL]))
    body = f"""Intel leadership has approved this deal. TCC can generate the SOW.

{deal_summary_block(deal)}

Next steps: TCC amends and issues the SOW. Proof of Performance items, including Cost Explorer, are collected after SOW signing.
{time.strftime('%B %d, %Y')}"""
    if send_email(recips, subject, body):
        log_email(deal, recips, subject)

def notify_sow_issued(deal):
    subject = f"IAP SOW Issued: {deal.get('custName', '')}"
    team = deal.get('team', [{}])
    submitter_email = team[0].get('email', '') if team else ''
    recips = list(dict.fromkeys(REVIEWER_EMAILS + ([submitter_email] if submitter_email else [])))
    body = f"""The SOW has been issued for this deal. Post-SOW execution moves to Smartsheet tracking.

{deal_summary_block(deal)}"""
    if send_email(recips, subject, body):
        log_email(deal, recips, subject)

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
    """Add the deal as a row on the IAP Project Intake Sheet.
    Submitter-derivable fields are populated now. TCC-owned fields
    (IPIC #, Activity Type, POP dates, Claim Quarter, Intel Budget Year,
    Contribution/Claimed/Paid/Remaining amounts, POP Received) are left
    unset so TCC fills them directly in Smartsheet post-submission.
    Column matching is tolerant by title — only columns that actually
    exist on the sheet get written to."""
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
        instances_used = deal.get('migTo', '') or ', '.join(
            f.get('instance', '') for f in deal.get('fleets', []) if f.get('instance'))
        is_migration = 'Yes' if str(deal.get('actType', '')).lower() in ('migrate', 'migration') else \
                       ('No' if deal.get('actType') else '')

        # Submitter-derivable fields — written now
        candidates = {
            'Activity Name': deal.get('dealName', '') or ('IAP - ' + deal.get('custName', '')),
            'IPIC Activity Description': deal.get('summaryText', '') or deal.get('projectDescription', ''),
            'Description': deal.get('summaryText', '') or deal.get('projectDescription', ''),
            'Migration Project Description': deal.get('projectDescription', ''),
            'Start Date': deal.get('migStart', ''),
            'End Date': deal.get('closeDate', '') or deal.get('migTargetDate', ''),
            'Partner or End Customer Name': deal.get('custName', ''),
            'Status': deal.get('status', ''),
            'Intake Entry Date': deal.get('submittedAt', '')[:10] if deal.get('submittedAt') else '',
            'Submitter Name': submitter.get('name', ''),
            'Submitter Email': submitter.get('email', ''),
            'Intel Rep Name': deal.get('intelRepName', ''),
            'AWS Rep Name': deal.get('awsRepName', ''),
            'Workload': deal.get('workload', '') or deal.get('custWorkload', ''),
            'Workload Selection': deal.get('workload', ''),
            'POP Available': 'No',
            'AWS Instances Used': instances_used,
            'Region/Country of Execution': deal.get('awsRegion', ''),
            'Cost of Infrastructure (ARR)': infra_arr,
            'Cost of Migration (Engineering Work)': float(deal.get('migrationCost', 0) or 0),
            'Requested funding amount': float(deal.get('dne', 0) or 0),
            'Funding amount not to exceed': float(deal.get('dne', 0) or 0),
            'Is this for a Migration activity?': is_migration,
            'Is this a migration activity?': is_migration,
            'Link to AWS Pricing Calculator': deal.get('pricingCalcLink', ''),
            'Expected ROI': deal.get('expectedRoi', ''),
            'AWS Alignment': deal.get('awsRegion', ''),
            'ACE Opportunity ID': deal.get('aceID', ''),
            'SFDC Opportunity ID': deal.get('sfdcID', ''),
            'Opportunity ID': deal.get('aceID', '') or deal.get('sfdcID', ''),
            # Legacy/simple sheet column names, kept for tolerance
            'Deal Name': deal.get('dealName', ''), 'Customer': deal.get('custName', ''),
            'Partner': deal.get('partnerName', ''), 'ACE ID': deal.get('aceID', ''),
            'Deal Type': deal.get('actType', ''), 'Target ARR': deal.get('targetArr', 0),
            'DNE': deal.get('dne', 0), 'Payment Option': deal.get('paymentOption', ''),
            'Migration Start': deal.get('migStart', ''),
            'Win Wire': 'Yes' if deal.get('winWire') else 'No',
            'IPIC Activity #': deal.get('ipicNum', ''),
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
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json'
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
                notify_submitted(deal)
                deal['smartsheetSync'] = push_to_smartsheet(deal)
            elif prev_status != curr_status:
                if curr_status == 'Approved (DNE Set)':
                    notify_intel(deal)
                elif curr_status == 'Intel Leadership Approved':
                    notify_intel_approved(deal)
                elif curr_status == 'SOW Issued':
                    notify_sow_issued(deal)

            table.put_item(Item=json.loads(json.dumps(deal), parse_float=str))
            return ok(headers, {'saved': True, 'id': deal['id'], 'status': deal.get('status', '')})

        # ── LIST DEALS ──
        if path == '/deals' and method == 'GET':
            resp = table.scan()
            items = [d for d in resp.get('Items', []) if not str(d.get('id', '')).startswith('config#')]
            return ok(headers, {'deals': items})

        # ── DNE CALC (server-side source of truth) ──
        if path == '/dne' and method == 'POST':
            dne = compute_dne(body.get('targetArr', 0), body.get('dealType', 'Migrate'))
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

        # ── AI: DOCUMENT AUTOFILL (any upload -> structured deal fields) ──
        if path == '/ai/extract' and method == 'POST':
            filename = body.get('filename', 'document')
            file_b64 = body.get('data', '')
            mime = body.get('mimeType', 'application/pdf')
            if not file_b64:
                return ok(headers, {'result': {'error': 'no file data received'}})
            raw_bytes = base64.b64decode(file_b64)
            doc_name = re.sub(r'[^a-zA-Z0-9\s()\[\]-]', ' ', filename.rsplit('.', 1)[0])[:50].strip() or 'document'

            prompt_text = """You are extracting deal information from an uploaded document for an Intel Accelerate Program (IAP) deal intake form.

Read the document carefully, including any email threads, tables, or slides. Extract only what is actually present — never invent or estimate a value that isn't stated.

Return ONLY valid JSON, no other text, no markdown fences, in this exact shape:
{
  "dealName": "", "custName": "", "partnerName": "",
  "actType": "one of: Migrate, Modernize, Optimize, or empty if unclear",
  "aceID": "", "targetArr": null, "migStart": "", "closeDate": "",
  "confidence": {"dealName":"high|medium|low","custName":"high|medium|low","partnerName":"high|medium|low","actType":"high|medium|low","aceID":"high|medium|low","targetArr":"high|medium|low","migStart":"high|medium|low","closeDate":"high|medium|low"},
  "flags": ["plain-sentence notes on contradictions, revisions across the document, or ambiguity worth a human checking"],
  "missingForARR": ["if targetArr could not be found or computed, list the specific facts still needed to get one — e.g. current AWS EC2 monthly spend on the target instance family, a Pricing Calculator estimate for the post-migration instances, or confirmation of what AWS services (vs. a managed third-party service like SAP RISE) actually carry the spend. Be concrete, not generic. Empty array if targetArr was found."]
}

Dates should be ISO format (YYYY-MM-DD) if a specific date is stated, otherwise leave empty. targetArr should be a number (no currency symbols or commas) only if an actual dollar figure is present — do not calculate or infer one from technical sizing data. If the document has nothing relevant to a field, leave it empty or null and mark its confidence "low". Flag anything that changes value across the document (e.g. a number revised in a later message) rather than silently picking one.

Note on managed/ISV deployments (e.g. SAP RISE, other managed private cloud editions): these frequently run on AWS infrastructure even though a third party like SAP manages and bills for the service — the deployment region and instance-level detail in the document (not the billing relationship) determines AWS eligibility. Do not assume a managed service is ineligible. If the document names an AWS region or shows memory/compute sizing without naming specific AWS EC2 instance types, say in missingForARR that the AWS EC2 instance types and pricing equivalent to the stated sizing still need to be identified (e.g. via AWS Pricing Calculator) — this is a translation gap, not a disqualification."""

            content_block = {"text": prompt_text}
            if mime.startswith('image/'):
                img_fmt = mime.split('/')[-1].replace('jpg', 'jpeg')
                doc_block = {"image": {"format": img_fmt, "source": {"bytes": raw_bytes}}}
            else:
                fmt_map = {
                    'application/pdf': 'pdf', 'text/plain': 'txt', 'text/csv': 'csv',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
                    'application/msword': 'doc',
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
                    'application/vnd.ms-excel': 'xls', 'text/html': 'html', 'text/markdown': 'md'
                }
                doc_fmt = fmt_map.get(mime, 'pdf')
                doc_block = {"document": {"format": doc_fmt, "name": doc_name, "source": {"bytes": raw_bytes}}}

            try:
                def _extract_with(model_id):
                    return bedrock_us.converse(
                        modelId=model_id,
                        messages=[{"role": "user", "content": [doc_block, content_block]}],
                        inferenceConfig={"maxTokens": 900, "temperature": 0.1}
                    )
                try:
                    response = _extract_with(NOVA_EXTRACT_MODEL)
                except Exception as first_err:
                    print(f"Primary extract model failed ({NOVA_EXTRACT_MODEL}): {str(first_err)}")
                    response = _extract_with(NOVA_EXTRACT_FALLBACK)
                text = response['output']['message']['content'][0]['text'].strip()
                if '```' in text:
                    text = text.split('```')[1]
                    if text.startswith('json'):
                        text = text[4:]
                    text = text.strip()
                result = json.loads(text)
            except Exception as e:
                print(f"Extraction error: {str(e)}")
                result = {'error': f'Could not read this file: {str(e)[:150]}'}
            return ok(headers, {'result': result})

        # ── AI: SOW CHECKER (kept) ──
        if path == '/ai/sow-check' and method == 'POST':
            deal = body.get('deal', {})
            prompt = f"""You are an expert reviewer for the Intel Accelerate Program (IAP).

Review this deal submission and identify issues that would cause rejection or delay at internal review.

Deal data:
- Deal name: {deal.get('dealName', 'Not provided')}
- Customer: {deal.get('custName', 'Not provided')}
- Partner: {deal.get('partnerName', 'Not provided')}
- ACE Opportunity ID: {deal.get('aceID', 'Not provided')}
- ACE Amount: ${deal.get('aceAmount', 0)}
- Deal type: {deal.get('actType', 'Not provided')}
- Payment option: {deal.get('paymentOption', 'Not provided')}
- Migration target date: {deal.get('migTargetDate', 'Not provided')}
- Target ARR: ${deal.get('targetArr', 0)}
- DNE: ${deal.get('dne', 0)}
- Simple Monthly Calculator attached: {'Yes' if deal.get('smcFileName') else 'No'}
- Win Wire: {'Yes' if deal.get('winWire') else 'No'}

Program rules:
- DNE = Target ARR x 70 percent (30 percent blended discount) x 4.5 percent (Migrate/Modernize) or 1 percent (Optimize, cap $250,000)
- ACE amount must equal deal amount
- Simple Monthly Calculator attachment is mandatory at submission
- Cost Explorer is NOT required at submission; it is collected by TCC after SOW signing
- Maximum duration 12 months, one calendar year
- 75 percent of target ARR is treated as full completion
- Eligible: {', '.join(ELIGIBLE_FAMILIES)} on EC2, RDS, ElastiCache, OpenSearch

Respond in this exact JSON format with no other text:
{{"score": <0-100>, "ready": <true|false>, "issues": ["..."], "warnings": ["..."], "recommendation": "one sentence"}}"""
            response = bedrock.invoke_model(
                modelId=NOVA_MODEL,
                body=json.dumps({
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "inferenceConfig": {"maxTokens": 600, "temperature": 0.1}
                })
            )
            result = json.loads(response['body'].read())
            text = result['output']['message']['content'][0]['text'].strip()
            if '```' in text:
                text = text.split('```')[1].replace('json', '').strip()
            return ok(headers, {'result': json.loads(text)})

        # ── AI: POP DRAFTER (kept; CE now post-SOW) ──
        if path == '/ai/pop-draft' and method == 'POST':
            deal = body.get('deal', {})
            submitter_name = deal.get('fhName', 'Account Owner')
            prompt = f"""You are drafting a professional email on behalf of Jacob Barksdale at The Channel Company requesting Proof of Performance for an Intel Accelerate Program deal after SOW signing.

Deal: {deal.get('custName', 'the customer')}, ACE {deal.get('aceID', 'TBD')}, DNE ${deal.get('dne', 0)}, migration target {deal.get('migTargetDate', 'TBD')}.

POP requirements:
1. AWS Cost Explorer export covering Intel instances ({', '.join(ELIGIBLE_FAMILIES)}) across EC2, RDS, ElastiCache, OpenSearch
   (Customer or Intel shares directly with TCC. AWS cannot access or transfer this data.)
2. Consumption evidence at the agreed thresholds for the selected payment option
3. Program limit: 12 months, one calendar year. 75 percent of target ARR is treated as complete.

Write a concise professional email to {submitter_name}. Dry, direct tone. Sign as Jacob Barksdale, The Channel Company. Respond with only the email body text."""
            response = bedrock.invoke_model(
                modelId=NOVA_MODEL,
                body=json.dumps({
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "inferenceConfig": {"maxTokens": 500, "temperature": 0.2}
                })
            )
            result = json.loads(response['body'].read())
            return ok(headers, {'email': result['output']['message']['content'][0]['text'].strip()})

        # ── AI: ASK NOVA (kept; brace bug fixed) ──
        if path == '/ai/ask' and method == 'POST':
            question = body.get('question', '')
            context_data = body.get('context', {})
            if not question:
                return ok(headers, {'answer': 'Please ask a question.'})
            prompt = f"""You are Nova, an expert assistant for the Intel Accelerate Program (IAP). This is an Intel program.

Key facts:
- DNE = Target ARR x 70 percent (30 percent blended discount budget) x 4.5 percent (Migrate/Modernize) or 1 percent (Optimize, capped $250,000)
- Payment options: Quarterly (10/20/30/40), Milestone (25/50/75), Lump Sum (at 75 percent or more)
- 75 percent of target ARR is treated as 100 percent complete
- Maximum duration 12 months, one calendar year
- Cost Explorer is collected by TCC after SOW signing, shared by Intel or the customer directly. AWS cannot access or transfer it.
- Simple Monthly Calculator attachment is required at submission. ACE amount must match deal amount.
- Eligible: {', '.join(ELIGIBLE_FAMILIES)} (i-suffix Intel families) on EC2, RDS, ElastiCache, OpenSearch. Non-Intel families excluded.
- Flow: Submitted > Under Review (Yasmine/Chris/Jeanine set DNE) > Intel Leadership Approved (one of Akanksha/Deep/Brendan) > SOW Issued (TCC) > In Progress > Complete

Context: step {context_data.get('currentStep', 'unknown')}, customer {context_data.get('custName', 'not set')}, type {context_data.get('actType', 'not set')}

Question: {question}

Answer clearly and concisely, under 150 words."""
            response = bedrock.invoke_model(
                modelId=NOVA_MODEL,
                body=json.dumps({
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "inferenceConfig": {"maxTokens": 300, "temperature": 0.3}
                })
            )
            result = json.loads(response['body'].read())
            return ok(headers, {'answer': result['output']['message']['content'][0]['text'].strip()})

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

        return {'statusCode': 404, 'headers': headers, 'body': json.dumps({'error': 'not found'})}

    except Exception as e:
        print(f"Handler error: {str(e)}")
        return {'statusCode': 500, 'headers': headers, 'body': json.dumps({'error': str(e)})}

def ok(headers, data):
    return {'statusCode': 200, 'headers': headers, 'body': json.dumps(data, default=str)}
