#!/usr/bin/env bash
# Export uv feed credentials from this job's OIDC token; see action.yml.
set -euo pipefail

# Not secrets: Entra checks the OIDC subject, not knowledge of these ids.
tenant=41d8b549-db3e-445e-bfee-a41de0552875
case "${ROLE}" in
  read) client=56153f3a-2099-4aa8-a079-2a0421db3889 ;;
  publish) client=9e3c2d95-b12d-4245-a5c6-345f398a4783 ;;
  *)
    echo "::error::role must be read or publish, not '${ROLE}'." >&2
    exit 1
    ;;
esac

if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
  echo "::error::No OIDC token: give this job 'permissions: id-token: write'." >&2
  exit 1
fi

# Print one JSON field, or the error text (it holds no secret) and fail.
field() {
  python3 -c '
import json, sys
body = json.load(sys.stdin)
if sys.argv[1] not in body:
    print(body.get("error_description") or body.get("message") or body, file=sys.stderr)
    sys.exit(1)
print(body[sys.argv[1]])' "$1"
}

jwt="$(curl -fsS -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=api://AzureADTokenExchange" | field value)"

# 499b84ac-... is Azure DevOps.
token="$(curl -sS "https://login.microsoftonline.com/${tenant}/oauth2/v2.0/token" \
  --data-urlencode "client_id=${client}" \
  --data-urlencode "scope=499b84ac-1321-427f-aa17-267ca6975798/.default" \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer" \
  --data-urlencode "client_assertion=${jwt}" | field access_token)"

echo "::add-mask::${token}"
{
  echo "UV_INDEX_FIRMFOOTING_USERNAME=firmfooting"
  echo "UV_INDEX_FIRMFOOTING_PASSWORD=${token}"
  if [ "${ROLE}" = publish ]; then
    echo "UV_PUBLISH_USERNAME=firmfooting"
    echo "UV_PUBLISH_PASSWORD=${token}"
  fi
} >> "${GITHUB_ENV}"
echo "Feed credentials exported for role ${ROLE}; the token lasts about an hour."
