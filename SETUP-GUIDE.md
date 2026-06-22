# IAP Backend Setup — AWS Console Click-Through
Account: 937991583695 · Suggested region: ca-central-1 (use the same region for all three steps)
Time: ~15 minutes. No code to write — only paste.

---

## STEP 1 — DynamoDB table (2 min)

1. Console → search **DynamoDB** → **Create table**
2. Table name: `iap-deals`
3. Partition key: `id` — type **String**
4. Leave everything else default → **Create table**

---

## STEP 2 — S3 bucket (2 min)

1. Console → search **S3** → **Create bucket**
2. Bucket name: `iap-cost-explorer-937991583695` (must be globally unique — add a suffix if taken)
3. Keep **Block all public access ON** (this data must stay private)
4. **Create bucket**

---

## STEP 3 — Lambda function (8 min)

1. Console → search **Lambda** → **Create function**
2. Choose **Author from scratch**
   - Function name: `iap-backend`
   - Runtime: **Python 3.12**
   - Leave the rest default → **Create function**
3. In the **Code** tab, delete everything in `lambda_function.py` and paste the contents of the `lambda_function.py` file I gave you → **Deploy**
4. **Configuration tab → Environment variables → Edit → Add:**
   - Key: `TABLE` Value: `iap-deals`
   - Key: `BUCKET` Value: `iap-cost-explorer-937991583695` (your exact bucket name)
   - Save
5. **Configuration tab → Permissions →** click the role name (opens IAM)
   - **Add permissions → Attach policies**
   - Attach: `AmazonDynamoDBFullAccess` and `AmazonS3FullAccess`
   - (Quick start. Hisham can tighten these to the single table/bucket later.)
6. Back in Lambda: **Configuration → Function URL → Create function URL**
   - Auth type: **NONE**
   - **Configure CORS: ON**
     - Allow origin: `*`
     - Allow methods: `*`
     - Allow headers: `content-type`
   - Save
7. **Copy the Function URL** — looks like `https://abc123.lambda-url.ca-central-1.on.aws`

---

## STEP 4 — Connect the app (1 min)

1. Open `index.html` in any text editor (TextEdit works)
2. Find the line near the bottom: `const API_URL = '';`
3. Paste your URL between the quotes — no trailing slash:
   `const API_URL = 'https://abc123.lambda-url.ca-central-1.on.aws';`
4. Save, re-zip with the two PDFs, drag-and-drop to Amplify

---

## What you get once connected

- **Submit to Core** writes the deal to DynamoDB — visible to anyone using the tool, not just your browser
- **Pipeline dashboard** reads from the cloud — your whole team sees the same deals
- **Cost Explorer uploads** land in the private S3 bucket, encrypted, organized by deal — never public

## Security notes (for Hisham)

- Function URL auth is NONE for speed — anyone with the URL can call it. Fine for a pilot; before customer scale, switch to AWS_IAM auth or add an API key check in the Lambda code.
- Replace the FullAccess policies with a scoped inline policy on just the `iap-deals` table and the one bucket.
- S3 bucket: enable default encryption (it's on by default now) and consider a lifecycle rule to archive files after 24 months.
