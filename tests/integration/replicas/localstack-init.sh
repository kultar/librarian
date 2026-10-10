#!/bin/sh
# Pre-create the secrets the replicas need (LocalStack "ready" hook).
awslocal secretsmanager create-secret --name lib/librarian/callers/agent-a --secret-string key-a
awslocal secretsmanager create-secret --name lib/service/echo --secret-string '{"write_token": "w-123", "other": "x"}'
