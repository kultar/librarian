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
      # - {name: path, in: path, allow_slash: true}   # a file path or git ref may contain "/"
    # approval: auto            # writes run without a person (never DELETE), up to
    # auto_limit_per_day: 100   # this many a day; chosen in the console or when enabling
      - {name: fields, in: query, description: Comma-separated fields}
  - id: searchPost
    method: POST
    path: /rest/api/3/search/jql
    access: read
    read_basis: reviewed          # required for non-GET reads; entry must be verified
    accepts_body: true
```

### Fields librarian sets when learning

```yaml
source:
  kind: docs                      # openapi | docs | inferred (model knowledge) | manual
  url: https://example.com/docs/api/
  pages: [https://example.com/docs/api/, https://example.com/docs/api/reference]
  content_hash: 3f1c...           # fingerprint used to notice changes
operations:
  - id: get_widgets_id
    origin: docs                  # spec | docs | model | manual
    evidence: "GET /v2/widgets/{id}"          # verbatim quote from evidence_url
    evidence_url: https://example.com/docs/api/
    confirmed_at: null            # set by a reviewer if wanted; runtime confirmations are stored
                                  # in the state database
    suspended: null               # a reason here refuses the operation until removed
    warning: null                 # shown to callers with every result
```

## Acting as the person

`auth.type: oauth2_user` makes librarian the OAuth client for the API and each person sign in
once (see the README's "Acting as the person"). Fields: `authorization_url`, `token_url`
(both https), `scopes`, `client_id`, `client_secret_ref` (optional; a secret reference, never
under `librarian/`), `pkce` (default true), `extra_authorize_params`. Only verified entries may
use it. The same type works for `write_auth`.

## Discovery and glossary

```yaml
discovery:                       # reads that teach instance-specific names
  - operation: getFields          # must be a read operation of this entry
    items_path: null              # where the list is (null = the response itself)
    key: id
    value: name
    key_prefix: customfield_      # keep only matching keys
glossary: {}                      # filled by discovery: {customfield_10020: Sprint, ...}
```

Operations may also carry `tags` (taken from the OpenAPI spec) to improve search.

## Writes

```yaml
status: verified                  # writes are never allowed on provisional entries
write_auth:                       # separate credential for writes (same shapes as `auth`)
  type: basic
  secret_ref: jira/write
operations:
  - id: addComment
    method: POST
    path: /rest/api/3/issue/{issueIdOrKey}/comment
    access: write
    writable: true                # opt in; every call still needs approval or a grant
    accepts_body: true
    rememberable: true            # default true; always false for DELETE
    parameters:
      - {name: issueIdOrKey, in: path}
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
| Operation `suspended` (in the entry, or by change detection) | No |

## Lifecycle

1. **Onboard**: `librarian learn <id> --spec-url ...` (or the `onboard` A2A skill) writes a
   provisional entry. Search-like POSTs are listed in `notes` for review. With publishing
   configured, a pull request with a review checklist is opened automatically.
2. **Review**: a human checks base URL, auth, and any operations to promote, then sets
   `status: verified` in the pull request. Once merged and synced, the running librarian
   hot-reloads the file. The review is approve or deny: the authentication and the operations
   that need a decision are settled in the console, and each decision lands as a commit on
   the open pull request. Branch protection on `main` (pull requests only, approving reviews,
   code owners) is compatible: librarian pushes only `librarian/*` branches, which must stay
   unprotected so the App can create them.
3. **Block**: denying a credential request sets `status: blocked` so agents stop asking.
4. **Change**: re-checks (`drift.recheck_hours`) compare the entry with its source. Provisional
   entries follow the source; for verified entries librarian opens an update pull request and
   suspends risky operations until it is merged (see the README).
