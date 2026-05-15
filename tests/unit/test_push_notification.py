"""Test push notification via on_match_found Lambda."""
import json
import boto3

REGION = "eu-south-1"
FUNCTION = "flot-on-match-found-dev"
PAYLOAD = {
    "detail": {
        "matchId": "test-001",
        "userId1": "46fea2c0-9051-7056-8bd7-b7bad07bf362",
        "userId2": "fake-test-passenger-001",
    }
}

client = boto3.client("lambda", region_name=REGION)
response = client.invoke(
    FunctionName=FUNCTION,
    InvocationType="RequestResponse",
    Payload=json.dumps(PAYLOAD),
)

result = json.loads(response["Payload"].read())
status = response["StatusCode"]
print(f"Status: {status}")
print(f"Response: {json.dumps(result, indent=2)}")
if response.get("FunctionError"):
    print(f"FunctionError: {response['FunctionError']}")
