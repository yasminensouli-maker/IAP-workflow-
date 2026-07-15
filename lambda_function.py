import json
import base64
import boto3
import time
import os
import re
import secrets
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
ses = boto3.client('ses', region_name='ca-central-1')
s3 = boto3.client('s3', region_name='ca-central-1')
# SMC attachments live in S3 (files don't belong in DynamoDB — 400KB item
# limit). The deal record stores only a reference (key + presigned URL). The
# bucket already exists in the account; the Lambda role needs s3:PutObject and
# s3:GetObject on it. Rebuilt securely (auth-gated, size-limited, sanitized
# filename) after the half-wired original was removed in the security build.
ATTACH_BUCKET = os.environ.get('ATTACH_BUCKET', 'iap-cost-explorer-937991583695')
MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_BYTES', str(15 * 1024 * 1024)))  # 15 MB
# Bedrock/Nova removed entirely — scoring and Q&A are deterministic logic in
# index.html (scoreFunding, answerIntelQuestion). S3 upload removed together
# with the attach-calculator requirement (July 2026 security/QA build).

# ── CONFIG (env vars — PRD Section 10; change via console, no code edit) ──
TABLE = os.environ.get('TABLE', 'iap-deals')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'yasmine@cloudzero.ca')
APP_URL = os.environ.get('APP_URL', 'https://main.dgxv59n7ru973.amplifyapp.com')

RATE_MIGRATE = float(os.environ.get('RATE_MIGRATE', '0.045'))      # Migrate / Modernize
RATE_OPTIMIZE = float(os.environ.get('RATE_OPTIMIZE', '0.01'))
OPTIMIZE_CAP = float(os.environ.get('OPTIMIZE_CAP', '250000'))
# BLENDED_DISCOUNT removed — it was a hidden 20% haircut applied underneath
# the visible math (the exact double-discount pattern this program's tooling
# has been burned by before). Discounts are applied once, visibly, in the
# Fleet Builder — never as an invisible server-side constant.
REVIEW_REMINDER_DAYS = int(os.environ.get('REVIEW_REMINDER_DAYS', '5'))
MILESTONE_LEAD_DAYS = int(os.environ.get('MILESTONE_LEAD_DAYS', '30'))
SESSION_TTL_SECONDS = int(os.environ.get('SESSION_TTL_SECONDS', str(12 * 3600)))  # matches the frontend's 12h
LOCKOUT_MAX_FAILS = 5           # failed logins per email before temporary lockout
LOCKOUT_WINDOW_SECONDS = 900    # 15 minutes
REMINDER_KEY = os.environ.get('REMINDER_KEY', '')  # shared secret for the EventBridge reminder call

# Intel pricing service — the key and endpoint now live in env vars only,
# never in source. If INTEL_PRICING_KEY is unset, the price route returns a
# clear config error instead of silently failing.
INTEL_PRICING_ENDPOINT = os.environ.get('INTEL_PRICING_ENDPOINT', 'http://52.26.245.170:8502/api/chat')
INTEL_PRICING_KEY = os.environ.get('INTEL_PRICING_KEY', '')

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
# to the browser. Passwords come ONLY from Lambda env vars. No defaults in
# source: an account whose env var is missing simply cannot log in (fails
# closed). Every zip of this code ever shared is now credential-free. ──
def _admin_pass(env_key):
    return os.environ.get(env_key) or None

ADMIN_USERS = {
    'yasmine@cloudzero.ca':        {'pass': _admin_pass('ADMIN_PASS_YASMINE'),  'tier':'admin', 'name':'Yasmine',        'label':'CloudZero Admin', 'approver':'core'},
    'reidelj@amazon.com':          {'pass': _admin_pass('ADMIN_PASS_JEANINE'),  'tier':'admin', 'name':'Jeanine Reidel', 'label':'AWS Approval (Admin)', 'approver':'core'},
    'clchrisz@amazon.com':         {'pass': _admin_pass('ADMIN_PASS_CHRIS'),    'tier':'core',  'name':'Chris Chlee',    'label':'AWS Approval (SA)', 'approver':'core'},
    'akanksha.r.bilani@intel.com': {'pass': _admin_pass('ADMIN_PASS_AKANKSHA'),'tier':'intel_approver','name':'Akanksha Bilani','label':'Intel Leadership','approver':'intel'},
    'brendon.roosken@intel.com':   {'pass': _admin_pass('ADMIN_PASS_BRENDON'), 'tier':'intel_approver','name':'Brendon Roosken','label':'Intel Leadership','approver':'intel'},
    'deep.grewal@intel.com':       {'pass': _admin_pass('ADMIN_PASS_DEEP'),    'tier':'intel_approver','name':'Deep Grewal',    'label':'Intel Leadership','approver':'intel'},
    'jacobx.barksdale@intel.com':  {'pass': _admin_pass('ADMIN_PASS_TCC'),     'tier':'tcc',   'name':'Jacob Barksdale','label':'TCC',             'approver':'tcc'},
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

def compute_dne(eligible_arr, deal_type):
    """Canonical funding formula — the single source of truth, mirrored by
    computeRate() in index.html:
        Funding = Eligible ARR x Rate. Migrate 4.5% uncapped;
        Modernize 1% capped at $250,000 per deal.
    Eligible ARR arrives already net of any visible, user-chosen discount —
    no hidden haircut is applied here (the old fixed 20% was removed).
    Unrecognized deal types get the CONSERVATIVE track (1%, capped): a rate
    default must never silently grant the more generous uncapped 4.5%."""
    arr = float(eligible_arr or 0)
    dt = str(deal_type or '').strip().lower()
    if dt.startswith('migrat'):
        return arr * RATE_MIGRATE
    return min(arr * RATE_OPTIMIZE, OPTIMIZE_CAP)

def compute_deal_dne(deal):
    """Recompute a deal's DNE server-side so the funded amount is never a
    client-asserted number. Basis, in priority order:
    1. intelEligibleArr (the field that drives DNE by design)
    2. the fleets' actualARR values (recomputed here, not trusted from
       the client's per-fleet 'rebate' figures), with the Modernize cap
       applied at the DEAL level."""
    eligible = float(deal.get('intelEligibleArr', 0) or 0)
    if eligible > 0:
        return round(compute_dne(eligible, deal.get('actType', '')), 2)
    fleets = deal.get('fleets') or []
    mig_total, mod_total = 0.0, 0.0
    for f in fleets:
        try:
            actual = float(f.get('actualARR', 0) or 0)
        except (TypeError, ValueError):
            actual = 0.0
        if str(f.get('type', '')).lower() == 'mod':
            mod_total += actual * RATE_OPTIMIZE
        else:
            mig_total += actual * RATE_MIGRATE
    mod_total = min(mod_total, OPTIMIZE_CAP)  # deal-level cap, not per-fleet
    return round(mig_total + mod_total, 2)

# ── SESSIONS & LOCKOUT (server-side auth — the login screen is no longer
# the only gate; every data route verifies a token issued at login) ──
def create_session(table, email, tier, name, label, approver):
    token = secrets.token_urlsafe(32)
    table.put_item(Item={
        'id': 'SESSION#' + token, 'email': email, 'tier': tier, 'name': name,
        'label': label, 'approver': approver or '',
        'expires': int(time.time()) + SESSION_TTL_SECONDS
    })
    return token

def get_session(event, table):
    """Return the session record for a valid Bearer token, else None."""
    auth = (event.get('headers', {}) or {}).get('authorization', '') or \
           (event.get('headers', {}) or {}).get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        item = table.get_item(Key={'id': 'SESSION#' + token}).get('Item')
    except Exception:
        return None
    if not item or int(float(item.get('expires', 0))) < int(time.time()):
        return None
    return item

def check_lockout(table, email):
    """True if this email is locked out from repeated failed logins."""
    try:
        item = table.get_item(Key={'id': 'FAIL#' + email}).get('Item')
    except Exception:
        return False
    if not item:
        return False
    if time.time() - float(item.get('firstAt', 0)) > LOCKOUT_WINDOW_SECONDS:
        return False
    return int(item.get('count', 0)) >= LOCKOUT_MAX_FAILS

def record_failed_login(table, email):
    try:
        item = table.get_item(Key={'id': 'FAIL#' + email}).get('Item') or {}
        if time.time() - float(item.get('firstAt', 0)) > LOCKOUT_WINDOW_SECONDS:
            item = {}
        table.put_item(Item={'id': 'FAIL#' + email,
                             'firstAt': item.get('firstAt', str(time.time())),
                             'count': int(item.get('count', 0)) + 1})
    except Exception as e:
        print(f"[LOCKOUT] failed to record attempt: {e}")

def clear_failed_logins(table, email):
    try:
        table.delete_item(Key={'id': 'FAIL#' + email})
    except Exception:
        pass

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
def notify_submitter(deal, curr_status):
    """A short status note to the person who submitted the deal, at every
    stage change. Sent as its OWN SES call, never bundled with approver
    recipients — in SES sandbox mode one unverified address rejects the whole
    send, and the approver chain must never fail because a field seller's
    inbox isn't verified. Failure here is logged on the deal and ignored."""
    team = deal.get('team') or []
    submitter = (team[0].get('email') if team and isinstance(team[0], dict) else '') or ''
    if not submitter or '@' not in submitter:
        return True  # nothing to send to; not an error
    stage_notes = {
        'Submitted': 'It is now with the AWS review team.',
        'Under Review': 'The AWS review team is working on it and setting the funding amount.',
        'Approved (DNE Set)': 'AWS review is complete and it is now with Intel Leadership for approval.',
        'Intel Leadership Approved': 'Intel Leadership has approved it. TCC will issue the SOW next.',
        'SOW Issued': 'The SOW has been issued. Watch for it via TCC and complete signature to start the funding schedule.',
    }
    note = stage_notes.get(curr_status)
    if not note:
        return True
    subject = f"Your IAP deal — {deal.get('custName', 'deal')}: {curr_status}"
    body = f"""Your Intel Accelerate Program deal for {deal.get('custName', '(customer pending)')} moved to: {curr_status}.

{note}

DNE on record: ${float(deal.get('dne', 0) or 0):,.0f}
Track status any time: {APP_URL}

This is an automated status note from the IAP Deal Desk.
{time.strftime('%B %d, %Y')}"""
    try:
        sent = send_email([submitter], subject, body)
        if sent:
            log_email(deal, [submitter], subject)
        else:
            deal.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': curr_status, 'note': f'Submitter status note to {submitter} failed — likely unverified in SES sandbox. Approver notifications unaffected.'})
        return sent
    except Exception as e:
        print(f"[SUBMITTER EMAIL] failed: {e}")
        return False

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
    ALLOWED_ORIGINS = [APP_URL, 'https://iapflow.com', 'https://www.iapflow.com'] + [
        o.strip() for o in os.environ.get('ALLOWED_ORIGINS', '').split(',') if o.strip()
    ]
    request_origin = event.get('headers', {}).get('origin', '') or event.get('headers', {}).get('Origin', '')
    # Exact allowlist only. The old '*.amplifyapp.com' suffix match accepted
    # ANY Amplify app from ANY AWS account worldwide, not just this one —
    # a hostile page on that domain could make credentialed browser calls
    # here. Every legitimate branch URL belongs in ALLOWED_ORIGINS (env var,
    # comma-separated), added deliberately, not matched by suffix.
    origin_is_valid = request_origin in ALLOWED_ORIGINS
    allow_origin = request_origin if origin_is_valid else APP_URL
    headers = {
        'Access-Control-Allow-Origin': allow_origin,
        'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        'Content-Type': 'application/json',
        'Vary': 'Origin'
    }
    # API Gateway can hand the Lambda either of two event shapes depending on
    # its integration payload-format setting: the newer one (rawPath +
    # requestContext.http.method) or the older one (path + httpMethod). Read
    # both so routing works regardless of which the API is actually sending —
    # a mismatch here made every route fall through to the 404 fallback.
    method = (event.get('requestContext', {}).get('http', {}) or {}).get('method') \
        or event.get('httpMethod') or 'GET'
    if method == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': '{}'}

    try:
        path = event.get('rawPath') or event.get('path') or '/'
        # Strip a trailing slash (but not the root path itself) so
        # '/auth/domain-login/' still matches '/auth/domain-login'.
        if len(path) > 1 and path.endswith('/'):
            path = path[:-1]
        body = {}
        if event.get('body'):
            raw = event['body']
            if event.get('isBase64Encoded'):
                raw = base64.b64decode(raw).decode('utf-8')
            body = json.loads(raw)
        table = dynamodb.Table(TABLE)

        # Resolve the caller's session once. Login routes don't need one;
        # every data route below checks `session` (and tier where relevant).
        session = get_session(event, table)

        def deny_unauthenticated():
            return {'statusCode': 401, 'headers': headers,
                    'body': json.dumps({'error': 'Your session has expired. Please sign in again.'})}

        def deny_tier(needed):
            return {'statusCode': 403, 'headers': headers,
                    'body': json.dumps({'error': f'This action requires {needed} access.'})}

        # ── SAVE DEAL (with audit diff + status-driven emails) ──
        if path == '/deal' and method == 'POST':
            if not session:
                return deny_unauthenticated()
            deal = body.get('deal', {})
            # The editor is whoever the token says it is — not a client field.
            editor = session.get('email', '')
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

            # An edit-save with a blank status keeps the stored status — a
            # deal's approval position is never erased by an incomplete form.
            if old_item and not deal.get('status'):
                deal['status'] = old_item.get('status', '')

            prev_status = old_item.get('status', '')
            curr_status = deal.get('status', '')

            # ── DNE is computed HERE, never accepted from the browser. ──
            # On submission: derived from Intel-Eligible ARR or the fleets.
            # On an explicit admin/core DNE-set: derived from the ARR basis
            # they entered (sent as dneBasisArr). On any other save: the
            # previously stored value is preserved, whatever the client sent.
            # A deal saved as a Draft first and submitted later must still get
            # its DNE computed. The old condition (not old_item) meant "never
            # saved before", which silently skipped DNE for any draft-then-
            # submit path — the deal would sit at $0 forever.
            was_already_submitted = bool(old_item) and old_item.get('status') not in ('', 'Draft', None)
            if body.get('submitted') and not was_already_submitted:
                deal['dne'] = compute_deal_dne(deal)
            elif body.get('dneBasisArr') is not None:
                if session.get('tier') not in ('admin', 'core'):
                    return deny_tier('AWS Approval')
                deal['dne'] = round(compute_dne(body.get('dneBasisArr', 0), deal.get('actType', '')), 2)
            elif old_item:
                deal['dne'] = old_item.get('dne', deal.get('dne', 0))

            # ── Approval-stage transitions require the right approver tier. ──
            if prev_status != curr_status and curr_status:
                required = {
                    'Approved (DNE Set)': ('admin', 'core'),
                    'Intel Leadership Approved': ('admin', 'intel_approver'),
                    'SOW Issued': ('admin', 'tcc'),
                }.get(curr_status)
                if required and session.get('tier') not in required:
                    return deny_tier(' / '.join(required))

            # Status-transition emails (PRD Section 8)
            # Same fix for notifications: a draft that is later submitted is a
            # real first submission and must notify approvers and reach
            # Smartsheet. Re-saving an ALREADY-submitted deal still must not
            # re-notify or reset its stage.
            if body.get('submitted') and not was_already_submitted:
                deal['status'] = curr_status = 'Submitted'
                deal['submittedAt'] = now_utc()
                deal['stageEnteredAt'] = deal['submittedAt']
                if not notify_submitted(deal):
                    deal.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': 'Submitted', 'note': 'Submit notification failed to send — check SES verification for recipients.'})
                notify_submitter(deal, 'Submitted')
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
                notify_submitter(deal, curr_status)

            table.put_item(Item=json.loads(json.dumps(deal), parse_float=str))
            return ok(headers, {'saved': True, 'id': deal['id'], 'status': deal.get('status', ''),
                                'dne': deal.get('dne', 0)})

        # ── LIST DEALS (any signed-in user) ──
        if path == '/deals' and method == 'GET':
            if not session:
                return deny_unauthenticated()
            resp = table.scan()
            items = [d for d in resp.get('Items', [])
                     if not str(d.get('id', '')).startswith(('config#', 'SESSION#', 'FAIL#', 'LOGIN#', 'AUTHCODE#'))]
            return ok(headers, {'deals': items})

        # ── LOGIN LOG — admin only ──
        if path == '/auth/login-log' and method == 'GET':
            if not session:
                return deny_unauthenticated()
            if session.get('tier') != 'admin':
                return deny_tier('admin')
            resp = table.scan()
            items = [d for d in resp.get('Items', []) if str(d.get('id', '')).startswith('LOGIN#')]
            items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            return ok(headers, {'logins': items})

        # ── DELETE A DEAL — permanent, no undo. Server-enforced: PRD Section 6
        # says Jacob and Yasmine, so tiers admin and tcc only. The tier comes
        # from the verified session token, not from anything the browser sent.
        if path == '/deals/delete' and method == 'POST':
            if not session:
                return deny_unauthenticated()
            if session.get('tier') not in ('admin', 'tcc'):
                return deny_tier('admin / TCC')
            deal_id = body.get('id')
            if not deal_id:
                return {'statusCode': 400, 'headers': headers, 'body': json.dumps({'error': 'Missing deal id.'})}
            existing = table.get_item(Key={'id': deal_id}).get('Item')
            print(f"[DELETE DEAL] id={deal_id} custName={(existing or {}).get('custName')} deletedBy={session.get('email')} existed={existing is not None}")
            table.delete_item(Key={'id': deal_id})
            return ok(headers, {'deleted': True, 'id': deal_id})

        # (The old /dne route is gone — it was never called by the frontend,
        # and it carried the hidden 20% haircut. DNE is now computed inside
        # the /deal save itself; see compute_deal_dne.)

        # ── Q&A LOG (PRD Stage 3) ──
        if path == '/question' and method == 'POST':
            if not session:
                return deny_unauthenticated()
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

        # (No frontend caller yet — kept, now auth-gated, for a future
        # in-app answer box. Answers currently happen over email.)
        if path == '/answer' and method == 'POST':
            if not session:
                return deny_unauthenticated()
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
            if not session:
                return deny_unauthenticated()
            if session.get('tier') not in ('admin', 'core', 'tcc'):
                return deny_tier('admin / AWS Approval / TCC')
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
            reminder_hdr = (event.get('headers', {}) or {}).get('x-reminder-key', '')
            is_scheduler = REMINDER_KEY and reminder_hdr == REMINDER_KEY
            if not is_scheduler and not (session and session.get('tier') == 'admin'):
                return deny_unauthenticated()
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
            if not session:
                return deny_unauthenticated()
            if session.get('tier') != 'admin':
                return deny_tier('admin')
            key = body.get('key', '')
            value = body.get('value', '')
            table.put_item(Item={'id': 'config#' + key, 'value': str(value), 'updatedAt': int(time.time())})
            return ok(headers, {'saved': True, 'key': key})
        if path == '/config' and method == 'GET':
            if not session:
                return deny_unauthenticated()
            key = event.get('queryStringParameters', {}).get('key', '') if event.get('queryStringParameters') else ''
            resp = table.get_item(Key={'id': 'config#' + key})
            return ok(headers, {'key': key, 'value': resp.get('Item', {}).get('value')})

        # (The /upload route and its S3 dependency were removed with the
        # attach-calculator requirement — it was never called by the frontend.)

        # ── UPLOAD SMC ATTACHMENT — signed-in users only. File goes to S3;
        # the deal keeps only a reference. Size-limited and filename-sanitized.
        if path == '/upload' and method == 'POST':
            if not session:
                return deny_unauthenticated()
            filename = (body.get('filename') or 'attachment').strip()
            filedata_b64 = body.get('data', '')
            deal_ref = body.get('dealRef', 'unassigned')
            if not filedata_b64:
                return ok(headers, {'error': 'No file data received.'})
            # Sanitize filename: strip paths, keep a safe character set.
            safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', filename.split('/')[-1].split('\\\\')[-1])[:120] or 'attachment'
            try:
                raw = base64.b64decode(filedata_b64)
            except Exception:
                return ok(headers, {'error': 'File could not be decoded. Please re-select and try again.'})
            if len(raw) > MAX_UPLOAD_BYTES:
                return ok(headers, {'error': f'File is too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB).'})
            key = f"smc/{re.sub(r'[^A-Za-z0-9_-]','_',str(deal_ref))}/{int(time.time())}-{safe_name}"
            try:
                s3.put_object(Bucket=ATTACH_BUCKET, Key=key, Body=raw, ServerSideEncryption='AES256')
                url = s3.generate_presigned_url('get_object',
                    Params={'Bucket': ATTACH_BUCKET, 'Key': key}, ExpiresIn=7*24*3600)
                return ok(headers, {'uploaded': True, 'key': key, 'url': url, 'filename': safe_name})
            except Exception as e:
                print(f"[UPLOAD] S3 put failed: {e}")
                return ok(headers, {'error': 'Upload failed — the storage bucket may need permission for the app. Contact yasmine@cloudzero.ca.'})

        # ── INTEL PRICING PROXY — signed-in users only, key from env var. ──
        if path == '/intel/price' and method == 'POST':
            if not session:
                return deny_unauthenticated()
            if not INTEL_PRICING_KEY:
                return ok(headers, {'error': 'Pricing service key not configured. Set INTEL_PRICING_KEY in the Lambda environment variables.'})
            import urllib.request as _ur
            message = body.get('message', '') or body.get('question', '')
            if not message:
                return ok(headers, {'error': 'no message'})
            req = _ur.Request(
                INTEL_PRICING_ENDPOINT,
                data=json.dumps({'message': message}).encode(),
                headers={'Content-Type': 'application/json',
                         'X-API-Key': INTEL_PRICING_KEY},
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
            if check_lockout(table, email):
                return {'statusCode': 429, 'headers': headers, 'body': json.dumps({'error': 'Too many failed attempts. Try again in 15 minutes.'})}
            admin = ADMIN_USERS.get(email)
            # No password material in logs — not even lengths. An account
            # whose env-var password was never set has pass=None and can
            # never authenticate (fails closed).
            if not admin or not admin['pass'] or not secrets.compare_digest(admin['pass'], password):
                print(f"[LOGIN FAIL] '{email}' — bad email or password")
                record_failed_login(table, email)
                return {'statusCode': 401, 'headers': headers, 'body': json.dumps({'error': 'Incorrect email or password.'})}
            print(f"[LOGIN OK] '{email}' tier={admin['tier']}")
            clear_failed_logins(table, email)
            notify_login(email, admin['tier'], admin['label'], 'password')
            token = create_session(table, email, admin['tier'], admin['name'], admin['label'], admin.get('approver'))
            return ok(headers, {
                'email': email, 'name': admin['name'], 'tier': admin['tier'],
                'label': admin['label'], 'approver': admin.get('approver'),
                'partnerFilter': admin.get('partnerFilter'), 'token': token
            })

        # ── AUTH: DOMAIN LOGIN — any real @amazon.com or @intel.com email,
        # one shared password. No pre-provisioned account needed. Deep and
        # Jacob get bumped to approver tier automatically by email even
        # though they're using the shared password like everyone else.
        DOMAIN_PASSWORD = os.environ.get('DOMAIN_PASSWORD')  # env only — no default in source
        if path == '/auth/domain-login' and method == 'POST':
            email = (body.get('email') or '').strip().lower()
            password = (body.get('password') or '').strip()
            which = (body.get('domain') or '').strip().lower()  # 'aws' or 'intel'
            expected_domain = 'amazon.com' if which == 'aws' else 'intel.com'
            actual_domain = email.split('@')[-1] if '@' in email else ''
            if actual_domain != expected_domain:
                return {'statusCode': 403, 'headers': headers,
                        'body': json.dumps({'error': f'Use a real @{expected_domain} email address.'})}
            if check_lockout(table, email):
                return {'statusCode': 429, 'headers': headers, 'body': json.dumps({'error': 'Too many failed attempts. Try again in 15 minutes.'})}
            if not DOMAIN_PASSWORD or not secrets.compare_digest(DOMAIN_PASSWORD, password):
                print(f"[DOMAIN LOGIN FAIL] '{email}'")
                record_failed_login(table, email)
                return {'statusCode': 401, 'headers': headers, 'body': json.dumps({'error': 'Incorrect password.'})}
            upgrade = DOMAIN_APPROVER_UPGRADES.get(email)
            tier = upgrade['tier'] if upgrade else ('aws' if which == 'aws' else 'intel')
            name = upgrade['name'] if upgrade else email.split('@')[0]
            label = upgrade['label'] if upgrade else ('AWS Field' if which == 'aws' else 'Intel Field')
            approver = upgrade.get('approver') if upgrade else None
            print(f"[DOMAIN LOGIN OK] '{email}' as tier={tier}")
            clear_failed_logins(table, email)
            notify_login(email, tier, label, 'domain password')
            token = create_session(table, email, tier, name, label, approver)
            return ok(headers, {'email': email, 'name': name, 'tier': tier, 'label': label, 'approver': approver, 'token': token})

        return {'statusCode': 404, 'headers': headers, 'body': json.dumps({'error': 'not found'})}

    except Exception as e:
        print(f"Handler error: {str(e)}")
        return {'statusCode': 500, 'headers': headers, 'body': json.dumps({'error': str(e)})}

def ok(headers, data):
    return {'statusCode': 200, 'headers': headers, 'body': json.dumps(data, default=str)}
