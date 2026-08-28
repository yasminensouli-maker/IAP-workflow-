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
# Where replies go. Mail can be sent from any address on a domain SES has
# verified, with no mailbox behind it -- but a reply to an address with no
# inbox vanishes silently, which in an approval chain means losing an
# "approved, go ahead". This routes replies somewhere a human reads.
REPLY_TO_EMAIL = os.environ.get('REPLY_TO_EMAIL', '').strip()
# Who gets the "someone just signed in" alert. Separate from FROM_EMAIL on
# purpose -- FROM_EMAIL is where mail is SENT FROM (now a no-mailbox sending
# address on the verified domain) and must never double as a recipient.
LOGIN_NOTIFY_EMAIL = os.environ.get('LOGIN_NOTIFY_EMAIL', 'yasmine@cloudzero.ca').strip()
APP_URL = os.environ.get('APP_URL', 'https://main.dgxv59n7ru973.amplifyapp.com')

RATE_MIGRATE = float(os.environ.get('RATE_MIGRATE', '0.045'))      # Migrate / Modernize
RATE_OPTIMIZE = float(os.environ.get('RATE_OPTIMIZE', '0.01'))
OPTIMIZE_CAP = float(os.environ.get('OPTIMIZE_CAP', '250000'))
# Migrate is capped too as of Aug 2026 — it used to be uncapped.
MIGRATE_CAP = float(os.environ.get('MIGRATE_CAP', '1000000'))
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
# Daily per-approver digest. Set DIGEST_ENABLED=0 to turn it off without a
# code change (useful while SES is still in sandbox and mail to @intel.com
# addresses is being rejected — see send_email).
DIGEST_ENABLED = os.environ.get('DIGEST_ENABLED', '1') not in ('0', 'false', 'False', '')

# Intel pricing service — the key and endpoint now live in env vars only,
# never in source. If INTEL_PRICING_KEY is unset, the price route returns a
# clear config error instead of silently failing.
INTEL_PRICING_ENDPOINT = os.environ.get('INTEL_PRICING_ENDPOINT', 'http://52.26.245.170:8502/api/chat')
INTEL_PRICING_KEY = os.environ.get('INTEL_PRICING_KEY', '')

# Approver emails — comma-separated env vars. Every default below is a real
# address so notifications reach the full team without depending on env-var
# config being present; CHRIS_EMAIL was previously empty, which silently left
# him off every reviewer notification.
REVIEWER_EMAILS = [e.strip() for e in os.environ.get(
    'REVIEWER_EMAILS', 'yasmine@cloudzero.ca,reidelj@amazon.com').split(',') if e.strip()]
CHRIS_EMAIL = os.environ.get('CHRIS_EMAIL', 'clchrisz@amazon.com').strip()
if CHRIS_EMAIL and CHRIS_EMAIL not in REVIEWER_EMAILS:
    REVIEWER_EMAILS.append(CHRIS_EMAIL)
# Appended the same way as CHRIS_EMAIL rather than added to the REVIEWER_EMAILS
# default string: if a REVIEWER_EMAILS env var is already set on the Lambda it
# overrides that default entirely, so editing the default would add him in
# source and change nothing in production.
BRYAN_EMAIL = os.environ.get('BRYAN_EMAIL', 'bryanofw@amazon.com').strip()
if BRYAN_EMAIL and BRYAN_EMAIL not in REVIEWER_EMAILS:
    REVIEWER_EMAILS.append(BRYAN_EMAIL)
DINC_EMAIL = os.environ.get('DINC_EMAIL', 'dinc@amazon.com').strip()
if DINC_EMAIL and DINC_EMAIL not in REVIEWER_EMAILS:
    REVIEWER_EMAILS.append(DINC_EMAIL)
INTEL_EMAILS = [e.strip() for e in os.environ.get(
    'INTEL_EMAILS', 'brendon.roosken@intel.com,deep.grewal@intel.com').split(',') if e.strip()]
TCC_EMAIL = os.environ.get('TCC_EMAIL', 'jacobx.barksdale@intel.com').strip()
ELIGIBLE_FAMILIES = [f.strip() for f in os.environ.get(
    'ELIGIBLE_FAMILIES', 'm8i,c8i,r8i,x8i').split(',') if f.strip()]

# ── GEO ROUTING ──
# Everyone on CORE_NOTIFY sees every deal. The geo owner is added on top,
# keyed off the GTM Region field the seller already fills in (fh-region), so
# no new field is needed. Matching is by prefix on the option text the form
# actually stores ("Americas - NAMER", "EMEA - MEA", "APJ - Japan", ...).
CORE_NOTIFY = [e.strip() for e in os.environ.get(
    'CORE_NOTIFY',
    'yasmine@cloudzero.ca,reidelj@amazon.com,brendon.roosken@intel.com,clchrisz@amazon.com'
).split(',') if e.strip()]

GEO_OWNERS = {
    'Americas - NAMER': [e.strip() for e in os.environ.get(
        'GEO_NAMER', 'brendon.roosken@intel.com,deep.grewal@intel.com').split(',') if e.strip()],
    'Americas - LATAM': [e.strip() for e in os.environ.get(
        'GEO_LATAM', 'f.pesiguelo@intel.com').split(',') if e.strip()],
    'EMEA': [e.strip() for e in os.environ.get(
        'GEO_EMEA', 'diego.bailon.humpert@intel.com').split(',') if e.strip()],
    'APJ': [e.strip() for e in os.environ.get(
        'GEO_APJ', 'jason.ct.tan@intel.com').split(',') if e.strip()],
}

# ── APPROVAL CHAIN ──
# Four gates. At each one, ANY single named approver can advance the deal —
# it does not wait for the others. Everyone on the notification list is
# copied at every stage regardless; what changes stage to stage is only who
# sees the approve button.
PREAPPROVAL_EMAILS = [e.strip() for e in os.environ.get(
    'PREAPPROVAL_EMAILS', 'yasmine@cloudzero.ca,clchrisz@amazon.com,reidelj@amazon.com'
).split(',') if e.strip()]
AWS_APPROVER_EMAILS = [e.strip() for e in os.environ.get(
    'AWS_APPROVER_EMAILS', 'bryanofw@amazon.com,dinc@amazon.com').split(',') if e.strip()]
INTEL_LEAD_EMAILS = [e.strip() for e in os.environ.get(
    'INTEL_LEAD_EMAILS', 'brendon.roosken@intel.com').split(',') if e.strip()]
# Can clear ANY gate, including one they are not the named approver for.
# Kept deliberately short — an override list that grows stops being an
# override and becomes the real approval model.
OVERRIDE_APPROVERS = [e.strip() for e in os.environ.get(
    'OVERRIDE_APPROVERS', 'brendon.roosken@intel.com,yasmine@cloudzero.ca').split(',') if e.strip()]

# status -> (human label for the gate, status it becomes once cleared)
APPROVAL_CHAIN = [
    ('Submitted',      'Pre-approval',   'Pre-Approved'),
    ('Pre-Approved',   'AWS approval',   'AWS Approved'),
    ('AWS Approved',   'Intel approval', 'Intel Approved'),
    ('Intel Approved', 'SOW creation',   'SOW Issued'),
]
CHAIN_BY_STATUS = {s: (label, nxt) for s, label, nxt in APPROVAL_CHAIN}

def stage_approvers(deal, status):
    """Who may clear the gate this deal is currently sitting at. The Intel
    gate is Brendon plus the geo owner for this deal's region, so an APJ deal
    can be cleared by Jason without waiting on Brendon."""
    if status == 'Submitted':
        return list(PREAPPROVAL_EMAILS)
    if status == 'Pre-Approved':
        return list(AWS_APPROVER_EMAILS)
    if status == 'AWS Approved':
        return list(dict.fromkeys(INTEL_LEAD_EMAILS + geo_owner_emails(deal)))
    if status == 'Intel Approved':
        # Jacob is not an approver. This gate is him issuing the SOW.
        return [TCC_EMAIL]
    return []

def can_approve(deal, email, status):
    who = str(email or '').strip().lower()
    if not who:
        return False
    if who in [e.lower() for e in OVERRIDE_APPROVERS]:
        return True
    return who in [e.lower() for e in stage_approvers(deal, status)]

def geo_owner_emails(deal):
    region = str(deal.get('region', '') or '').strip()
    for prefix, emails in GEO_OWNERS.items():
        if region.startswith(prefix):
            return list(emails)
    return []

def geo_recipients(deal):
    """Core list plus the owner for this deal's GTM Region, plus whoever
    entered the deal. An unrecognised or blank region falls back to core only
    — never to an empty list, because a deal that notifies nobody looks
    exactly like a deal nobody has gotten to yet."""
    region = str(deal.get('region', '') or '').strip()
    owners = []
    for prefix, emails in GEO_OWNERS.items():
        if region.startswith(prefix):
            owners = emails
            break
    # The submitter's address is stamped server-side from the session at
    # submit time. The deal team array is only a fallback: nothing in the
    # form requires an email there, so relying on it alone silently drops
    # the submitter off their own deal's notifications.
    submitter = str(deal.get('submitterEmail', '') or '').strip()
    if not submitter:
        for t in (deal.get('team') or []):
            if isinstance(t, dict) and t.get('email'):
                submitter = str(t['email']).strip()
                break
    # Everyone sees every stage: the core four, every named approver at every
    # gate (so Bryan and Dinc watch a deal arrive rather than being surprised
    # by it), the geo owner, Jacob, and the submitter. Authority is handled
    # separately in can_approve() — being on this list grants no rights.
    everyone = (CORE_NOTIFY + PREAPPROVAL_EMAILS + AWS_APPROVER_EMAILS
                + INTEL_LEAD_EMAILS + owners
                + ([submitter] if submitter else []) + [TCC_EMAIL])
    return [e for e in dict.fromkeys(everyone) if e]

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
    'brendon.roosken@intel.com':   {'pass': _admin_pass('ADMIN_PASS_BRENDON'), 'tier':'admin','name':'Brendon Roosken','label':'Intel Leadership (Admin)','approver':'intel'},
    # Geo approvers. They clear the Intel gate for their own theatre, so they
    # need a real login — being named in GEO_OWNERS grants authority in the
    # backend but the screen never offered them the button.
    'diego.bailon.humpert@intel.com': {'pass': _admin_pass('ADMIN_PASS_DIEGO'), 'tier':'intel_approver','name':'Diego Bailon Humpert','label':'Intel Leadership (EMEA)','approver':'intel'},
    'jason.ct.tan@intel.com':      {'pass': _admin_pass('ADMIN_PASS_JASON'),   'tier':'intel_approver','name':'Jason Tan',    'label':'Intel Leadership (APJ)','approver':'intel'},
    'f.pesiguelo@intel.com':       {'pass': _admin_pass('ADMIN_PASS_FABIO'),   'tier':'intel_approver','name':'Fabio Pesiguelo','label':'Intel Leadership (LATAM)','approver':'intel'},
    'deep.grewal@intel.com':       {'pass': _admin_pass('ADMIN_PASS_DEEP'),    'tier':'intel_approver','name':'Deep Grewal',    'label':'Intel Leadership','approver':'intel'},
    'jacobx.barksdale@intel.com':  {'pass': _admin_pass('ADMIN_PASS_TCC'),     'tier':'tcc',   'name':'Jacob Barksdale','label':'TCC',             'approver':'tcc'},
    'bryanofw@amazon.com':         {'pass': _admin_pass('ADMIN_PASS_BRYAN'),   'tier':'admin', 'name':'Bryan',          'label':'AWS Approval (Admin)', 'approver':'core'},
    'dinc@amazon.com':             {'pass': _admin_pass('ADMIN_PASS_DINC'),    'tier':'admin', 'name':'Dinc',           'label':'AWS Approval (Admin)', 'approver':'core'},
}

# People who log in through the open @amazon.com / @intel.com domain buttons
# but need approver-level access rather than the generic seller tier —
# checked by email after the shared domain password succeeds.
DOMAIN_APPROVER_UPGRADES = {
    'brendon.roosken@intel.com':  {'tier':'admin',          'name':'Brendon Roosken','label':'Intel Leadership (Admin)','approver':'intel'},
    'deep.grewal@intel.com':      {'tier':'intel_approver','name':'Deep Grewal',    'label':'Intel Leadership','approver':'intel'},
    'diego.bailon.humpert@intel.com':{'tier':'intel_approver','name':'Diego Bailon Humpert','label':'Intel Leadership (EMEA)','approver':'intel'},
    'jason.ct.tan@intel.com':     {'tier':'intel_approver','name':'Jason Tan',      'label':'Intel Leadership (APJ)','approver':'intel'},
    'f.pesiguelo@intel.com':      {'tier':'intel_approver','name':'Fabio Pesiguelo','label':'Intel Leadership (LATAM)','approver':'intel'},
    'jacobx.barksdale@intel.com': {'tier':'tcc',            'name':'Jacob Barksdale','label':'TCC',             'approver':'tcc'},
    # Without this line Bryan could sign in through the @amazon.com domain
    # button and land on the generic seller tier — able to log in, unable to
    # approve, with nothing on screen explaining why.
    'bryanofw@amazon.com':        {'tier':'admin',          'name':'Bryan',          'label':'AWS Approval (Admin)','approver':'core'},
    'dinc@amazon.com':            {'tier':'admin',          'name':'Dinc',           'label':'AWS Approval (Admin)','approver':'core'},
}

# PRD Section 7 statuses. Old stage values map forward for existing records.
STATUS_MAP_OLD_TO_NEW = {
    'core': 'Submitted',
    'intel': 'AWS Approved',
    'tcc': 'Intel Approved',
    'approved': 'SOW Issued',
    'changes_requested': 'Submitted',
    # Statuses from the three-gate chain, mapped onto the four-gate one.
    # Without these rows an existing deal sits at a status no gate recognises,
    # which reads on screen as "no approve button for anyone" — the deal is
    # not stuck, it is unreachable.
    'Under Review': 'Submitted',
    'Approved (DNE Set)': 'AWS Approved',
    'Intel Leadership Approved': 'Intel Approved',
}

def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def send_email(to_addresses, subject, body_text):
    try:
        kwargs = {
            'Source': FROM_EMAIL,
            'Destination': {'ToAddresses': to_addresses},
            'Message': {
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body_text, 'Charset': 'UTF-8'}}
            }
        }
        # Only set it when configured. An empty ReplyToAddresses list is fine,
        # but a list containing an empty string is rejected by SES.
        if REPLY_TO_EMAIL:
            kwargs['ReplyToAddresses'] = [REPLY_TO_EMAIL]
        ses.send_email(**kwargs)
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
        Funding = Eligible ARR x Rate. Migrate 4.5% capped at $1,000,000;
        Modernize 1% capped at $250,000. Both tracks are capped per deal.
    Eligible ARR arrives already net of any visible, user-chosen discount —
    no hidden haircut is applied here (the old fixed 20% was removed).
    Unrecognized deal types get the CONSERVATIVE track (1%, capped): a rate
    default must never silently grant the more generous uncapped 4.5%."""
    arr = float(eligible_arr or 0)
    dt = str(deal_type or '').strip().lower()
    if dt.startswith('migrat'):
        return min(arr * RATE_MIGRATE, MIGRATE_CAP)
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

# ── DAILY PER-APPROVER DIGEST ──
# Who owns a deal at each status. The digest is built from this one map so a
# deal can never appear in two people's queues, or in nobody's. If a new
# status is added to the workflow, add it here too or deals at that status
# become invisible to the digest — which looks exactly like "no work pending".
def digest_owner_emails(status):
    if status in ('Submitted', 'Under Review'):
        return list(REVIEWER_EMAILS), 'AWS Approval'
    if status == 'Approved (DNE Set)':
        return list(INTEL_EMAILS), 'Intel Leadership'
    if status == 'Intel Leadership Approved':
        return [TCC_EMAIL], 'TCC'
    if status == 'SOW Issued':
        # Still Jacob's: the SOW is out and Proof of Performance is collected
        # against it. Not pending approval, but pending someone.
        return [TCC_EMAIL], 'TCC — SOW issued, POP outstanding'
    return [], ''

# What has to be present before a deal reaches TCC. Jacob issues the SOW from
# these fields, so a gap here is a gap he has to chase by email. Kept as a
# named list rather than inline checks so the list is editable in one place
# when the program's requirements change.
SOW_REQUIRED_FIELDS = [
    ('dealName',      'Deal name'),
    ('custName',      'Customer name'),
    ('aceID',         'ACE opportunity ID'),
    ('actType',       'Track (Migrate or Modernize)'),
    ('paymentOption', 'Payment option'),
    ('migStart',      'Migration start date'),
    ('migTo',         'Target Intel instance family'),
]

def missing_for_sow(deal):
    """Field labels a deal is missing before TCC can issue the SOW."""
    gaps = [label for key, label in SOW_REQUIRED_FIELDS
            if not str(deal.get(key, '') or '').strip()]
    if not (float(deal.get('dne', 0) or 0) > 0):
        gaps.append('Funding amount (DNE) is zero')
    return gaps

def days_in_stage(deal, now_ts):
    stamp = deal.get('stageEnteredAt') or deal.get('submittedAt') or ''
    try:
        entered = datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return None
    return int((now_ts - entered) / 86400)

def scan_all_deals(table):
    """Full paginated scan. table.scan() alone returns only the first 1MB, so
    a single-page scan starts silently dropping deals as the table grows —
    the digest would look complete while omitting the oldest records."""
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        last = resp.get('LastEvaluatedKey')
        if not last:
            return items
        kwargs['ExclusiveStartKey'] = last

def digest_body(person_email, rows):
    lines = [f"{len(rows)} deal{'' if len(rows) == 1 else 's'} waiting on you.", '']
    for r in rows:
        d, waited = r['deal'], r['days']
        age = 'today' if waited in (0, None) else f"{waited} day{'' if waited == 1 else 's'}"
        lines.append(f"{d.get('custName') or d.get('dealName') or 'Unnamed deal'}"
                     f"  —  ${float(d.get('dne', 0) or 0):,.0f}  —  waiting {age}")
        lines.append(f"  Stage: {r['stage']}")
        gaps = r['gaps']
        if gaps:
            lines.append(f"  Incomplete: {', '.join(gaps)}")
        lines.append('')
    lines.append(f"Sign in to review: {APP_URL}")
    return '\n'.join(lines)

def send_daily_digest(table):
    """One email per approver listing only the deals at their stage, oldest
    first. Sent per-recipient rather than as one group send: in SES sandbox a
    single unverified address rejects the whole call, so bundling would mean
    one bad address silences everyone. Nobody with an empty queue gets mail —
    a daily 'nothing pending' email is what teaches people to filter these
    into a folder and stop reading them."""
    now_ts = time.time()
    queues = {}
    for d in scan_all_deals(table):
        if str(d.get('id', '')).startswith(('config#', 'SESSION#', 'FAIL#', 'LOGIN#', 'AUTHCODE#', 'DECISION#')):
            continue
        status = d.get('status', '')
        owners, stage_label = digest_owner_emails(status)
        if not owners:
            continue
        row = {'deal': d, 'stage': stage_label, 'days': days_in_stage(d, now_ts),
               'gaps': missing_for_sow(d)}
        for who in owners:
            if who:
                queues.setdefault(who, []).append(row)

    results = []
    for who, rows in queues.items():
        # Oldest first — the point of the digest is that the thing sitting
        # longest is the thing read first.
        rows.sort(key=lambda r: (-(r['days'] if r['days'] is not None else 0)))
        subject = f"IAP: {len(rows)} deal{'' if len(rows) == 1 else 's'} waiting on you"
        results.append({'to': who, 'count': len(rows),
                        'sent': send_email([who], subject, digest_body(who, rows))})
    return results

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

def log_decision(table, deal, stage_label, action, decider_email, decider_name, reason):
    """Every approve and every decline writes one row here, permanently.
    This is the precedent record -- not a model, a log. It exists so a
    future deal's funding case can be checked against what actually got
    approved or declined before, with the reasons attached, instead of
    starting from nothing every time. Never overwritten, never deleted by
    the app -- one item per decision, keyed so it can't collide with a
    deal record or any other prefixed item in this table.
    """
    try:
        table.put_item(Item=json.loads(json.dumps({
            'id': f'DECISION#{deal.get("id","")}#{int(time.time()*1000)}#{secrets.token_hex(3)}',
            'dealId': deal.get('id', ''),
            'custName': deal.get('custName', ''),
            'at': now_utc(),
            'stage': stage_label,
            'action': action,              # 'approved' or 'declined'
            'by': decider_email,
            'byName': decider_name,
            'reason': reason,
            # Funding-case snapshot at the moment of this decision, so a
            # later query can ask "what did we approve/decline that cited
            # Migration ARR Won" without needing the full deal record.
            'program': deal.get('actType', ''),
            'eligibleArr': deal.get('intelEligibleArr') or deal.get('targetArr') or deal.get('aceAmount') or 0,
            'fundingRoi': deal.get('fundingRoi', 0),
            'strategicFactors': deal.get('strategicFactors', ''),
            'rippleFactors': deal.get('rippleFactors', ''),
        }), parse_float=str))
    except Exception as e:
        # A logging failure must never block the actual approval/decline --
        # the deal's own status write already succeeded or is about to.
        print(f"[DECISION LOG] failed: {e}")

def notify_declined(deal, stage_label, declined_by, reason):
    """The one email the reject path was missing entirely: without this, a
    decline changed the deal's status and wrote a comment, but told nobody —
    not the submitter, not the other approvers. Uses geo_recipients() so the
    audience is identical to every other stage email: core four, every named
    approver at every gate, the region's Intel owner, the submitter, and
    Jacob."""
    subject = f"IAP Deal Declined at {stage_label}: {deal.get('custName', 'Deal')}"
    body = f"""{declined_by} declined this deal at {stage_label} and sent it back to the submitter.

Reason: {reason}

{deal_summary_block(deal)}

The submitter can revise and resubmit — it re-enters at Pre-approval.
{time.strftime('%B %d, %Y')}"""
    recips = geo_recipients(deal)
    ok_sent = send_email(recips, subject, body)
    if ok_sent:
        log_email(deal, recips, subject)
    return ok_sent

def notify_submitted(deal):
    subject = f"IAP Deal Submitted: {deal.get('custName', 'New Deal')}"
    body = f"""A new deal has been submitted to the Intel Accelerate Program and is pending internal review.

{deal_summary_block(deal)}

Next step: review the deal, run the DNE calculator, and approve to route to Intel leadership.
{time.strftime('%B %d, %Y')}"""
    # Geo-routed: core four on every deal, plus the theatre owner, plus the
    # person who entered it, plus Jacob. Replaces the old flat REVIEWER_EMAILS
    # blast, which mailed the same four people regardless of where the deal was.
    sub_recips = geo_recipients(deal)
    ok_sent = send_email(sub_recips, subject, body)
    if ok_sent:
        log_email(deal, sub_recips, subject)
    return ok_sent

def notify_intel(deal):
    subject = f"IAP Deal Pending Intel Approval: {deal.get('custName', 'New Deal')} — DNE ${float(deal.get('dne',0) or 0):,.0f}"
    body = f"""A deal has cleared internal review. The DNE is set. One approval from Intel leadership is required.

{deal_summary_block(deal)}

Reply through the app: approve, or ask a question. Questions are logged against the deal record.
{time.strftime('%B %d, %Y')}"""
    # Jacob (TCC) is copied on every funding approval: he owns SOW issuance and
    # chases the collection items, so he needs the DNE when it is set, not only
    # once Intel has approved. dict.fromkeys de-dupes if he is already listed.
    recips = list(dict.fromkeys(INTEL_EMAILS + [TCC_EMAIL]))
    ok_sent = send_email(recips, subject, body)
    if ok_sent:
        log_email(deal, recips, subject)
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
                    # 'dne' is deliberately skipped here: at this point it still
                    # holds whatever the browser sent, and the server has not yet
                    # computed the real value. It is audited below, after
                    # compute, so the trail records the number actually stored.
                    if f in deal and f != 'dne':
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
            # INVARIANT: the stored DNE must ALWAYS equal its stated basis x
            # rate. Previously an edit-save preserved the old DNE verbatim, so
            # correcting the Intel-Eligible ARR left the DNE frozen at the old
            # value while the UI's own "how this was calculated" text showed
            # the NEW arr x rate — the record contradicted itself and understated
            # real deals. Recompute on every save; an explicit admin DNE-set
            # also writes its basis back to intelEligibleArr so the two can
            # never drift apart again. Every change is captured in auditLog.
            if body.get('dneBasisArr') is not None:
                if session.get('tier') not in ('admin', 'core'):
                    return deny_tier('AWS Approval')
                try:
                    basis = float(body.get('dneBasisArr') or 0)
                except (TypeError, ValueError):
                    basis = 0.0
                # Store the basis as the eligible ARR so the displayed
                # explanation is literally true of the stored number.
                deal['intelEligibleArr'] = basis
                deal['dne'] = round(compute_dne(basis, deal.get('actType', '')), 2)
            else:
                deal['dne'] = compute_deal_dne(deal)

            # Audit the DNE against what was actually stored before — this runs
            # after compute so the trail reflects the real number, not the
            # browser's suggestion. Without this, a change to the funded amount
            # left no record of who moved it or when.
            if old_item:
                audit(deal, editor, 'dne', old_item.get('dne'), deal.get('dne'))

            # ── Approval-stage transitions require the right approver tier. ──
            # Authority is now per-person and per-gate, not per-tier. The check
            # is against the stage the deal is LEAVING: the question is "may
            # you clear the gate it is sitting at", not "may you touch the
            # stage it is moving to". Tier checks could not express "any one
            # of Yasmine, Chris or Jeanine" or "Jason but only for APJ".
            decision = str(body.get('decision', '') or '').strip().lower()
            if decision == 'reject':
                # Sends the deal back to intake, not one step back. The chain
                # only names a forward next-status per gate, so "the step
                # before this one" is not a single well-defined place -- and a
                # rejected deal usually needs the submitter's attention, not
                # a silent re-queue at whatever stage happens to precede it.
                gate = CHAIN_BY_STATUS.get(prev_status)
                gate_label = gate[0] if gate else prev_status
                if not can_approve(deal, editor, prev_status):
                    allowed = ', '.join(stage_approvers(deal, prev_status))
                    return {'statusCode': 403, 'headers': headers, 'body': json.dumps({
                        'error': f"{gate_label} is cleared by: {allowed}."})}
                note = str(body.get('comment', '') or '').strip()
                if not note:
                    return {'statusCode': 400, 'headers': headers, 'body': json.dumps({
                        'error': 'A reason is required to decline a deal.'})}
                deal.setdefault('comments', []).append({
                    'at': now_utc(), 'by': editor, 'name': session.get('name', editor),
                    'stage': gate_label, 'action': 'declined', 'text': note,
                })
                deal['status'] = curr_status = 'Submitted'
                deal['stageEnteredAt'] = now_utc()
                deal['lastRejection'] = {'at': now_utc(), 'by': session.get('name', editor),
                                          'stage': gate_label, 'reason': note}
                if not notify_declined(deal, gate_label, session.get('name', editor), note):
                    deal.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': gate_label,
                        'note': 'Decline notification failed to send — check SES verification.'})
                log_decision(table, deal, gate_label, 'declined', editor, session.get('name', editor), note)
            elif prev_status != curr_status and curr_status:
                gate = CHAIN_BY_STATUS.get(prev_status)
                if gate:
                    gate_label, legal_next = gate
                    if curr_status != legal_next:
                        return {'statusCode': 400, 'headers': headers, 'body': json.dumps({
                            'error': f"A deal at '{prev_status}' can only move to '{legal_next}'."})}
                    if not can_approve(deal, editor, prev_status):
                        allowed = ', '.join(stage_approvers(deal, prev_status))
                        return {'statusCode': 403, 'headers': headers, 'body': json.dumps({
                            'error': f"{gate_label} is cleared by: {allowed}."})}
                    # Comment box. Required on a rejection, optional on an
                    # approval, always attributed and always kept — the record
                    # of why a deal moved is the thing people ask for later.
                    note = str(body.get('comment', '') or '').strip()
                    deal.setdefault('comments', []).append({
                        'at': now_utc(), 'by': editor,
                        'name': session.get('name', editor),
                        'stage': gate_label, 'action': 'approved',
                        'text': note,
                    })
                    log_decision(table, deal, gate_label, 'approved', editor, session.get('name', editor), note)

            # Status-transition emails (PRD Section 8)
            # Same fix for notifications: a draft that is later submitted is a
            # real first submission and must notify approvers and reach
            # Smartsheet. Re-saving an ALREADY-submitted deal still must not
            # re-notify or reset its stage.
            if body.get('submitted') and not was_already_submitted:
                deal['status'] = curr_status = 'Submitted'
                deal['submittedAt'] = now_utc()
                deal['stageEnteredAt'] = deal['submittedAt']
                # Stamped from the session, not from a form field: this is the
                # address every later notification copies, and a seller who
                # leaves their own email box blank must not fall off the thread.
                if not deal.get('submitterEmail'):
                    deal['submitterEmail'] = session.get('email', '')
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
                     if not str(d.get('id', '')).startswith(('config#', 'SESSION#', 'FAIL#', 'LOGIN#', 'AUTHCODE#', 'DECISION#'))]
            return ok(headers, {'deals': items})

        # ── DECISION HISTORY — every approve/decline, ever. Powers the
        # precedent lookup in the Funding Case panel: "3 similar deals
        # citing this goal were approved, 1 declined." Paginated scan
        # because scan_all_deals already exists and this table can grow
        # past the single-page 1MB limit the same way /deals can. ──
        if path == '/decisions' and method == 'GET':
            if not session:
                return deny_unauthenticated()
            all_items = scan_all_deals(table)
            decisions = [d for d in all_items if str(d.get('id', '')).startswith('DECISION#')]
            return ok(headers, {'decisions': decisions})

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
            # Same gap as the login notifier: notify_question() returns False
            # on an SES failure and nothing checked it, so a question could
            # sit unread with zero signal to anyone. Logged to the deal now,
            # same as every other notify_* call site in this file.
            if not notify_question(item, question, asked_by):
                item.setdefault('emailFailures', []).append({'at': now_utc(), 'stage': item.get('status', ''),
                    'note': 'Question notification to reviewers failed to send — check SES verification.'})
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
            # Daily per-approver digest. Guarded by a date marker so a double
            # EventBridge fire, a retry, or an admin hitting this route by hand
            # cannot mail everyone twice in one day. Pass ?force=1 to override
            # when testing.
            digest = []
            force_digest = str((event.get('queryStringParameters') or {}).get('force', '')) in ('1', 'true')
            if DIGEST_ENABLED:
                today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                marker = {}
                try:
                    marker = table.get_item(Key={'id': 'config#digest'}).get('Item') or {}
                except Exception:
                    marker = {}
                if force_digest or marker.get('lastSentOn') != today_str:
                    digest = send_daily_digest(table)
                    try:
                        table.put_item(Item={'id': 'config#digest', 'lastSentOn': today_str,
                                             'recipients': len(digest), 'at': now_utc()})
                    except Exception as e:
                        print(f"Digest marker write failed: {str(e)}")

            sent = []
            resp = table.scan()
            now_ts = time.time()
            for d in resp.get('Items', []):
                if str(d.get('id', '')).startswith(('config#', 'DECISION#')):
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
            return ok(headers, {'remindersSent': sent, 'digest': digest})

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
            # Was sent to FROM_EMAIL, which is the SENDING address, not a
            # recipient. That was invisible while FROM_EMAIL happened to equal
            # Yasmine's own inbox; the moment it was changed to
            # noreply@iapflow.com (to fix Intel's spam filters rejecting mail
            # from the unauthenticated cloudzero.ca domain), every login
            # notification started addressing itself to a mailbox that does
            # not exist, and nobody saw an error because send_email() to a
            # valid-format address does not fail. Notified party is now its
            # own setting, independent of whichever address mail is sent FROM.
            notify_to = LOGIN_NOTIFY_EMAIL or REPLY_TO_EMAIL or FROM_EMAIL
            # Was a bare fire-and-forget call, unlike every other notify_*
            # function in this file. send_email() swallows SES errors and
            # returns False on failure -- a caller that doesn't check that
            # return value gets total silence: not an exception, not a log
            # line, nothing. Login almost always succeeds even when the
            # notification email doesn't, so nobody would ever have seen this.
            if not send_email([notify_to], f'IAP Deal Desk sign-in — {email}',
                       f'{email} signed in just now.\n\nTier: {tier} ({label})\nMethod: {via}\nTime (UTC): {now_utc()}'):
                print(f"[LOGIN NOTIFY FAILED] could not email {notify_to} about sign-in by {email} "
                      f"-- check SES sandbox status and whether {notify_to} is a verified identity in ca-central-1.")
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
        # These are the people who don't rotate: pre-approval (Yasmine, Chris,
        # Jeanine), AWS approval (Bryan, Dinc), Intel approval (Brendon plus the
        # regional owners Deep, Fabio, Diego, Jason), and TCC (Jacob).
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
