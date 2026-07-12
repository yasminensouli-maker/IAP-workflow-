{
    "FunctionName": "iap-backend",
    "FunctionArn": "arn:aws:lambda:ca-central-1:937991583695:function:iap-backend",
    "Runtime": "python3.12",
    "Role": "arn:aws:iam::937991583695:role/iap-backend-role",
    "Handler": "lambda_function.lambda_handler",
    "CodeSize": 13298,
    "Description": "",
    "Timeout": 30,
    "MemorySize": 128,
    "LastModified": "2026-07-12T23:27:01.000+0000",
    "CodeSha256": "2b6g7VXbWsKcVEkYB71fqeu+K19wxSZqpRIHealBrDU=",
    "Version": "$LATEST",
    "Environment": {
        "Variables": {
            "TABLE": "iap-deals",
            "ADMIN_PASS_CHRIS": "sapphire27**",
            "SMARTSHEET_SHEET_ID": "4182169990680452",
            "ADMIN_PASS_BRENDON": "sapphire27**",
            "BUCKET": "iap-cost-explorer-937991583695",
            "ADMIN_PASS_AKANKSHA": "sapphire27**",
            "ADMIN_PASS_TCC": "sapphire27**",
            "ADMIN_PASS_DEEP": "sapphire27**",
            "ADMIN_PASS_YASMINE": "sapphire27**",
            "INTEL_PRICING_KEY": "intel-arch-7f3a9c2e8b14d05f6a1e9d7c3b8f240a",
            "ADMIN_PASS_JEANINE": "sapphire27**",
            "DOMAIN_PASSWORD": "XEON6@AWS"
        }
    },
    "TracingConfig": {
        "Mode": "PassThrough"
    },
    "RevisionId": "7dbc1757-3360-4bbe-aaca-216607dcfced",
    "State": "Active",
    "LastUpdateStatus": "InProgress",
    "LastUpdateStatusReason": "The function is being created.",
    "LastUpdateStatusReasonCode": "Creating",
    "PackageType": "Zip",
    "Architectures": [
        "x86_64"
    ],
    "EphemeralStorage": {
        "Size": 512
    },
    "SnapStart": {
        "ApplyOn": "None",
        "OptimizationStatus": "Off"
    },
    "RuntimeVersionConfig": {
        "RuntimeVersionArn": "arn:aws:lambda:ca-central-1::runtime:40182b778d40c8bdb13a6ef86990df74f5066cdb7d40aac1845f6f3fa5a1b20f"
    },
    "LoggingConfig": {
        "LogFormat": "Text",
        "LogGroup": "/aws/lambda/iap-backend"
    }
}
