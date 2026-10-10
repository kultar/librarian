# Registry format

Each API is one YAML file named `<id>.yaml` in the registry directory. Unknown fields are errors.

```yaml
id: jira                          # lowercase letters, digits, '-'; must match the file name
name: Jira Cloud
description: Issues, projects and users.
base_url: https://your-site.atlassian.net
status: verified                  # provisional | verified | blocked

auth:                             # one of:
  type: basic                     #   none
  secret_ref: jira/readonly       #   bearer   {secret_ref}
                                  #   api_key  {secret_ref, in: header|query, name}
                                  #   basic    {secret_ref}  secret holds "username:password"

credential:                       # shown to the approver on the secure page
  create_url: https://id.atlassian.com/manage-profile/security/api-tokens
  scopes: [read:jira-work]
  instructions: Use a dedicated service account with browse-only permissions.
  validation_operation: getCurrentUser   # a cheap read used to test new keys

owners:                           # notification routing (optional)
  channel: "#atlassian-admins"
  approver_group: S0123ABCD       # Slack user-group ID → real @mention

operations:
  - id: getIssue
    method: GET
    path: /rest/api/3/issue/{issueIdOrKey}
    summary: Get an issue
    access: read                  # read | write | unknown
    parameters:
      - {name: issueIdOrKey, in: path}
      - {name: fields, in: query, description: Comma-separated fields}
  - id: searchPost
    method: POST
    path: /rest/api/3/search/jql
    access: read
    read_basis: reviewed          # required for non-GET reads; entry must be verified
    accepts_body: true
```

## Pagination

Declare it on the entry (default for every operation) or per operation. Without it, `Link:
rel="next"` headers are followed. Callers ask for more pages with `max_pages` (1-20).

```yaml
pagination:                 # Jira search: send nextPageToken back until it disappears
  style: cursor
  cursor_param: nextPageToken
  next_cursor_path: nextPageToken
  items_path: issues        # pages are merged into one list here
```

| Style | Needs | Stops when |
|---|---|---|
| `link` | nothing | no `rel="next"` link |
| `cursor` | `cursor_param`, `next_cursor_path` | no cursor in the response |
| `offset` | `offset_param`, `items_path` (`total_path` optional) | empty page or offset ≥ total |
| `page` | `page_param`, `items_path` (`first_page`, default 1) | empty page |

## Field selection

Callers can pass `select` with dotted paths to keep only what they need. Lists are traversed
automatically: `["issues.key", "issues.fields.summary"]` returns just the key and summary of every
issue. This keeps large responses under the size cap and saves LLM context.

## Rules enforced by the policy engine

| Situation | Allowed? |
|---|---|
| `access: read`, GET/HEAD, provisional or verified entry | Yes |
| `access: read`, `read_basis: reviewed` (e.g. POST search), verified entry | Yes |
| Same, provisional entry | No |
| `access: write` or `unknown` | No |
| Entry `blocked` | No |
| Undeclared argument, missing required argument | No |
| Path argument containing `/`, `.` or `..` | No |
| Header argument named Authorization/Cookie/Host/X-Api-Key | No |

## Lifecycle

1. **Onboard**: `librarian learn <id> --spec-url ...` (or the `onboard` A2A skill) writes a
   provisional entry. Search-like POSTs are listed in `notes` for review. With publishing
   configured, a pull request with a review checklist is opened automatically.
2. **Review**: a human checks base URL, auth, and any operations to promote, then sets
   `status: verified` in the pull request. Once merged and synced, the running librarian
   hot-reloads the file.
3. **Block**: denying a credential request sets `status: blocked` so agents stop asking.
