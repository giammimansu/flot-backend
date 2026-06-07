"""Manual smoke script — invokes the deployed on_match_found Lambda.

NOT a pytest test: it calls real AWS. Guarded under __main__ so pytest collection
does not trigger an AWS call. Run explicitly:  python tests/unit/test_push_notification.py
"""
import json

REGION = "eu-south-1"
FUNCTION = "flot-on-match-found-dev"
PAYLOAD = {
    "detail": {
        "matchId": "test-001",
        "userId1": "46fea2c0-9051-7056-8bd7-b7bad07bf362",
        "userId2": "fake-test-passenger-001",
    }
}


def main() -> None:
    import boto3

    client = boto3.client("lambda", region_name=REGION)
    response = client.invoke(
        FunctionName=FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(PAYLOAD),
    )
    result = json.loads(response["Payload"].read())
    print(f"Status: {response['StatusCode']}")
    print(f"Response: {json.dumps(result, indent=2)}")
    if response.get("FunctionError"):
        print(f"FunctionError: {response['FunctionError']}")


if __name__ == "__main__":
    main()
