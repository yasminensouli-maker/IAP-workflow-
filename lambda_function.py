import json
import base64
import boto3
import time
import os

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
ses = boto3.client('ses', region_name='ca-central-1')
bedrock = boto3.client('bedrock-runtime', region_name='ca-central-1')

TABLE = os.environ.get('TABLE', 'iap-deals')
BUCKET = os.environ.get('BUCKET', '')
NOVA_MODEL = 'amazon.nova-lite-v1:0'
FROM_EMAIL = 'yasmine@cloudzero.ca'
APP_URL = 'https://main.dgxv59n7ru973.amplifyapp.com'

def send_email(to_addresses, subject, body_text):
    """Send email via SES. to_addresses is a list."""
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
        print(f"SES error: {str(e)}")
        return False

def notify_intel_approvers(deal):
    """Fire when ASP Core approves — email Intel Leadership."""
    subject = f"IAP Deal Pending Your Approval: {deal.get('custName', 'New Deal')}"
    rebate = float(deal.get('rebate', 0))
    body = f"""Hi Akanksha, Brendon, and Deep,

A deal has been approved by ASP Core and is now pending Intel Leadership approval.

Deal details:
- Customer: {deal.get('custName', '')}
- Activity Type: {deal.get('actType', '')}
- ACE Opportunity ID: {deal.get('aceID', 'Pending')}
- IPIC Activity #: {deal.get('ipicNum', 'TBD')}
- Estimated Benefit: ${rebate:,.2f}
- Expected ROI (10x): ${rebate * 10:,.2f}

Please log in to the IAP Workflow to review and approve:
{APP_URL}

Credentials:
- Akanksha: akanksha.r.bilani@intel.com / Intel2026
- Brendon: brendon.roosken@intel.com / Intel2026
- Deep: deep.grewal@intel.com / Intel2026

Approved by ASP Core
{time.strftime('%B %d, %Y')}
"""
    send_email(
        ['akanksha.r.bilani@intel.com', 'brendon.roosken@intel.com', 'deep.grewal@intel.com'],
        subject,
        body
    )

def notify_tcc(deal):
    """Fire when Intel Leadership approves — email Jacob at TCC."""
    subject = f"IAP Deal Ready for TCC Processing: {deal.get('custName', 'New Deal')}"
    rebate = float(deal.get('rebate', 0))
    body = f"""Hi Jacob,

A deal has cleared both ASP Core and Intel Leadership approval and is ready for TCC processing.

Deal details:
- Customer: {deal.get('custName', '')}
- Activity Type: {deal.get('actType', '')}
- ACE Opportunity ID: {deal.get('aceID', 'Pending')}
- IPIC Activity #: {deal.get('ipicNum', 'TBD')}
- Estimated Benefit: ${rebate:,.2f}
- Cost Explorer: {deal.get('ceFileName', 'Not attached')}

Please log in to the IAP Workflow to complete final approval and POP check:
{APP_URL}

Credentials: jacobx.barksdale@intel.com / TCC2026

{time.strftime('%B %d, %Y')}
"""
    send_email(
        ['jacobx.barksdale@intel.com'],
        subject,
        body
    )

def notify_submitter_approved(deal):
    """Fire when fully approved — email the submitter."""
    team = deal.get('team', [{}])
    submitter_email = team[0].get('email', '') if team else ''
    submitter_name = team[0].get('name', 'Submitter') if team else 'Submitter'
    if not submitter_email:
        return
    subject = f"IAP Deal Approved: {deal.get('custName', 'Your Deal')}"
    rebate = float(deal.get('rebate', 0))
    body = f"""Hi {submitter_name},

Your IAP deal has been fully approved and is now onboarding with The Channel Company.

Deal details:
- Customer: {deal.get('custName', '')}
- Estimated Benefit: ${rebate:,.2f}
- Expected ROI (10x): ${rebate * 10:,.2f}

Next steps: The Channel Company (Jacob Barksdale) will reach out regarding the SOW and payment schedule.

You can track status at:
{APP_URL}

IAP Workflow
"""
    send_email([submitter_email], subject, body)

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

        # ── SAVE DEAL ──
        if path == '/deal' and method == 'POST':
            deal = body.get('deal', {})
            if not deal.get('id'):
                deal['id'] = str(int(time.time() * 1000))
            deal['updatedAt'] = int(time.time())

            # Check if approval stage changed — fire SES notifications
            prev_stage = body.get('prevStage', '')
            curr_stage = deal.get('approvalStage', '')

            table.put_item(Item=json.loads(json.dumps(deal), parse_float=str))

            # Fire emails on stage transitions
            if prev_stage == 'core' and curr_stage == 'intel':
                notify_intel_approvers(deal)
            elif prev_stage == 'intel' and curr_stage == 'tcc':
                notify_tcc(deal)
            elif curr_stage == 'approved':
                notify_submitter_approved(deal)

            return ok(headers, {'saved': True, 'id': deal['id']})

        # ── LIST DEALS ──
        if path == '/deals' and method == 'GET':
            resp = table.scan()
            items = [d for d in resp.get('Items', []) if not str(d.get('id', '')).startswith('config#')]
            return ok(headers, {'deals': items})

        # ── CONFIG (budget pool) ──
        if path == '/config' and method == 'POST':
            key = body.get('key', '')
            value = body.get('value', '')
            table.put_item(Item={'id': 'config#' + key, 'value': str(value), 'updatedAt': int(time.time())})
            return ok(headers, {'saved': True, 'key': key})

        if path == '/config' and method == 'GET':
            key = event.get('queryStringParameters', {}).get('key', '') if event.get('queryStringParameters') else ''
            resp = table.get_item(Key={'id': 'config#' + key})
            item = resp.get('Item', {})
            return ok(headers, {'key': key, 'value': item.get('value')})

        # ── SOW CHECKER ──
        if path == '/ai/sow-check' and method == 'POST':
            deal = body.get('deal', {})
            prompt = f"""You are an expert reviewer for the Intel Acceleration Program (IAP), an AWS co-sell funding program.

Review this deal submission and identify any issues that would cause rejection or delay at APO Core review.

Deal data:
- Customer: {deal.get('custName', 'Not provided')}
- ACE Opportunity ID: {deal.get('aceID', 'Not provided')}
- IPIC Activity #: {deal.get('ipicNum', 'Not provided')}
- Activity Type: {deal.get('actType', 'Not provided')}
- Migration Start: {deal.get('migStart', 'Not provided')}
- Migration End / Close: {deal.get('closeDate', 'Not provided')}
- AWS Segment: {deal.get('segment', 'Not provided')}
- Region: {deal.get('region', 'Not provided')}
- Workload: {deal.get('workloadSelection', 'Not provided')}
- Estimated Benefit (rebate): ${deal.get('rebate', 0)}
- Expected ROI: ${float(deal.get('rebate', 0)) * 10}
- Fleet count: {len(deal.get('fleets', []))}
- ACE Stage: {deal.get('aceStage', 'Not provided')}
- Submitter: {deal.get('fhName', 'Not provided')}
- Campaign Code: {deal.get('campaignCode', 'Not provided')}

IAP Program rules:
- Migration rate: 4.5% of actual post-discount ARR
- Optimization rate: 1% of ARR, NTE $250,000
- Payment is milestone-based: signed roadmap triggers first payment, Cost Explorer at 75%+ triggers final
- Maximum duration: 12 months, calendar year only
- AWS cannot share customer Cost Explorer data — customer must send directly to The Channel Company
- Deal must be in ACE co-sell status before submission
- IPIC Activity # is mandatory
- ACE Opportunity ID is mandatory

Respond in this exact JSON format with no other text:
{{
  "score": <0-100 readiness score>,
  "ready": <true or false>,
  "issues": ["issue 1", "issue 2"],
  "warnings": ["warning 1"],
  "recommendation": "one sentence recommendation"
}}"""

            response = bedrock.invoke_model(
                modelId=NOVA_MODEL,
                body=json.dumps({
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "inferenceConfig": {"maxTokens": 600, "temperature": 0.1}
                })
            )
            result = json.loads(response['body'].read())
            text = result['output']['message']['content'][0]['text']
            text = text.strip()
            if '```' in text:
                text = text.split('```')[1].replace('json','').strip()
            ai_result = json.loads(text)
            return ok(headers, {'result': ai_result})

        # ── POP DRAFTER ──
        if path == '/ai/pop-draft' and method == 'POST':
            deal = body.get('deal', {})
            submitter_name = deal.get('fhName', 'Account Owner')
            customer = deal.get('custName', 'the customer')
            ace_id = deal.get('aceID', 'TBD')
            ipic = deal.get('ipicNum', 'TBD')
            rebate = deal.get('rebate', 0)
            mig_start = deal.get('migStart', 'TBD')
            close_date = deal.get('closeDate', 'TBD')

            prompt = f"""You are drafting a professional email on behalf of Jacob Barksdale at The Channel Company to request Proof of Performance (POP) for an Intel Acceleration Program (IAP) deal.

Deal details:
- Customer: {customer}
- Account Owner / Submitter: {submitter_name}
- ACE Opportunity ID: {ace_id}
- IPIC Activity #: {ipic}
- Estimated Benefit: ${rebate}
- Migration Start: {mig_start}
- Expected Close: {close_date}

POP requirements:
1. Signed Migration Roadmap from the customer
2. AWS Cost Explorer export showing EC2 spend at 75% or greater completion
   (Customer must export from AWS Console: Billing → Cost Explorer → EC2 → Group by Instance Type → export CSV)
   (Customer sends directly to The Channel Company and Intel — AWS cannot share this data)
3. Payment cannot exceed 12 months from migration start, calendar year only

Write a concise, professional email from Jacob Barksdale to {submitter_name} requesting the POP.
- Dry, direct tone — no marketing language
- Include the two POP requirements clearly
- Reference the deal by customer name and ACE ID
- Remind them of the 12-month calendar-year payment deadline
- Sign off as Jacob Barksdale, The Channel Company

Respond with ONLY the email body text, no subject line, no JSON wrapper."""

            response = bedrock.invoke_model(
                modelId=NOVA_MODEL,
                body=json.dumps({
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "inferenceConfig": {"maxTokens": 500, "temperature": 0.2}
                })
            )
            result = json.loads(response['body'].read())
            email_text = result['output']['message']['content'][0]['text'].strip()
            return ok(headers, {'email': email_text})

        # ── UPLOAD COST EXPLORER FILE ──
        if path == '/upload' and method == 'POST':
            filename = body.get('filename', 'file')
            filedata = body.get('data', '')
            deal_id = body.get('dealId', 'unassigned')
            key = f"cost-explorer/{deal_id}/{int(time.time())}-{filename}"
            s3.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=base64.b64decode(filedata),
                ServerSideEncryption='AES256'
            )
            return ok(headers, {'uploaded': True, 'key': key})

        # ── ASK NOVA (general IAP assistant) ──
        if path == '/ai/ask' and method == 'POST':
            question = body.get('question', '')
            context = body.get('context', {})
            if not question:
                return ok(headers, {'answer': 'Please ask a question.'})

            prompt = f"""You are Nova, an expert assistant for the Intel Acceleration Program (IAP), an AWS co-sell funding program. You help AWS sellers, Intel field reps, and partners understand and navigate the IAP.

Key IAP facts:
- Migration rate: 4.5% of customer's actual post-discount EC2 ARR on Intel-eligible instances
- Optimization/Modernization rate: 1% of ARR, capped at $250,000 NTE
- Payment is milestone-based: signed migration roadmap triggers first payment; Cost Explorer at 75%+ completion triggers final payment
- Maximum duration: 12 months, calendar year only
- Cost Explorer must be sent directly by the customer to The Channel Company and Intel — AWS cannot share customer spend data
- Deal must be in ACE co-sell status before submission
- IPIC Activity # is mandatory for all deals
- Intel-eligible instances: m8i, c8i, r8i, m7i, c7i, r7i, m6i, c6i, r6i and other Intel Xeon families
- Trainium and Graviton workloads are NOT eligible
- Payment flows through The Channel Company (TCC) — AWS does not participate in payment
- Jacob Barksdale at TCC handles invoicing
- Approval chain: ASP Core (Yasmine/Jeanine) → Intel Leadership (Akanksha/Brendon/Deep) → TCC (Jacob)
- Stackable with MAP, VMware SPI, Greenfield SPI, and PoC funding

Current context:
- Step: {context.get('currentStep', 'unknown')}
- Customer: {context.get('custName', 'not set')}
- Activity type: {context.get('actType', 'not set')}

User question: {question}

Answer clearly and concisely. If the question is about a specific field in the form, explain what to enter and why. Keep answers under 150 words. Do not use bullet points unless listing more than 3 items."""

            response = bedrock.invoke_model(
                modelId=NOVA_MODEL,
                body=json.dumps({{
                    "messages": [{{"role": "user", "content": [{{"text": prompt}}]}}],
                    "inferenceConfig": {{"maxTokens": 300, "temperature": 0.3}}
                }})
            )
            result = json.loads(response['body'].read())
            answer = result['output']['message']['content'][0]['text'].strip()
            return ok(headers, {{'answer': answer}})

        # ── INTEL PRICING PROXY ──
        if path == '/intel/price' and method == 'POST':
            import urllib.request as _ur
            message = body.get('message', '')
            if not message:
                return ok(headers, {'error': 'no message'})
            intel_payload = json.dumps({'message': message}).encode()
            req = _ur.Request(
                'http://52.26.245.170:8502/api/chat',
                data=intel_payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-API-Key': 'intel-arch-7f3a9c2e8b14d05f6a1e9d7c3b8f240a'
                },
                method='POST'
            )
            with _ur.urlopen(req, timeout=15) as intel_resp:
                intel_data = json.loads(intel_resp.read().decode())
            return ok(headers, intel_data)

        # ── DEBUG: return Lambda outbound IP ──
        if path == '/debug/ip' and method == 'GET':
            import urllib.request as _ur2
            with _ur2.urlopen('https://checkip.amazonaws.com', timeout=5) as r:
                outbound_ip = r.read().decode().strip()
            return ok(headers, {{'lambda_outbound_ip': outbound_ip}})

        return {{'statusCode': 404, 'headers': headers, 'body': json.dumps({{'error': 'not found'}})}}

    except Exception as e:
        return {'statusCode': 500, 'headers': headers, 'body': json.dumps({'error': str(e)})}

def ok(headers, data):
    return {'statusCode': 200, 'headers': headers, 'body': json.dumps(data, default=str)}
