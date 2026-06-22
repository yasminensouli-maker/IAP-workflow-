import json
import base64
import boto3
import time
import os

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
bedrock = boto3.client('bedrock-runtime', region_name='us-east-1')

TABLE = os.environ.get('TABLE', 'iap-deals')
BUCKET = os.environ.get('BUCKET', '')
NOVA_MODEL = 'amazon.nova-pro-v1:0'

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
            table.put_item(Item=json.loads(json.dumps(deal), parse_float=str))
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
            # Clean and parse JSON
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

        return {'statusCode': 404, 'headers': headers, 'body': json.dumps({'error': 'not found'})}

    except Exception as e:
        return {'statusCode': 500, 'headers': headers, 'body': json.dumps({'error': str(e)})}

def ok(headers, data):
    return {'statusCode': 200, 'headers': headers, 'body': json.dumps(data, default=str)}
