# Google Ads MCP Server

This repo contains the source code for running an
[MCP](https://modelcontextprotocol.io) server that interacts with the
[Google Ads API](https://developers.google.com/google-ads/api).

## Tools

The server uses the
[Google Ads API](https://developers.google.com/google-ads/api/reference/rpc/latest/overview)
to provide several
[Tools](https://modelcontextprotocol.io/docs/concepts/tools) and [Resources](https://modelcontextprotocol.io/docs/concepts/tools) for use with LLMs and AI agents.

### Tools available

- `search`: Retrieves information about the Google Ads account.
- `get_resource_metadata`: Retrieves metadata about a Google Ads API resource type, for example "campaign". This is useful to understand the structure of the data and what fields are available for querying.
- `list_accessible_customers`: Returns ids of customers directly accessible
  by the user authenticating the call.
- `publish_recommendation`: Publishes a validated, evidence-backed proposal to
  the Constellation Google Ads Recommendation Center. This action never writes
  to Google Ads and never approves or executes a recommendation.
- `sync_customer_catalog`: Reads enabled non-manager descendants of the login
  MCC and synchronizes them to the Recommendation Center without enrolling any
  account.
- `get_due_enrollments`: Returns a bounded portal-managed queue of accounts due
  for recommendation analysis.
- `record_enrollment_run`: Records an idempotent scheduler outcome and advances
  the enrolled account's next eligible run time. It never modifies Google Ads.

### Recommendation Center publishing

The Constellation fork adds portal-only recommendation and enrollment actions.
They are disabled by missing configuration. Configure these Cloud Run
environment variables:

- `RECOMMENDATION_CENTER_URL`: Fixed HTTPS origin for the Recommendation Center.
- `RECOMMENDATION_CENTER_INGESTION_KEY`: Secret used by the Center to authenticate ingestion.
- `RECOMMENDATION_CENTER_SIWC_BYPASS_TOKEN`: Secret used by server-to-server requests to pass the Sites sign-in gate.
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`: Manager customer id used as the trust anchor for recommendation publishing. A requested customer must be an enabled, non-manager account directly or indirectly beneath this MCC.
- `RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS` (optional): Comma-separated customer ids that further restrict publishing to a pilot subset of the MCC hierarchy. Leave unset to authorize all verified client accounts beneath the MCC.

Each publication keeps its submitted recommendation id for retry compatibility
and can also include a stable semantic identity: rule, affected resources,
observed condition, and proposed target state. The Recommendation Center uses
that identity to return one canonical recommendation across analysis runs,
refresh an unresolved item, reopen a later regression, or suppress a lifecycle
duplicate. A successful ingestion confirms `published: true`, both submitted
and canonical ids, an explicit publication outcome, and
`google_ads_changes_made: false`. Only
`counts_as_new_recommendation: true` contributes to
`record_enrollment_run.recommendation_count`; refreshed and suppressed items do
not create another review card.

Completed enrollment runs also report the most recent complete account-local
date and evidence notes for all ten required review areas: conversion tracking,
budget and pacing, bidding, delivery, campaign structure, ads and assets,
targeting and traffic quality, search terms, landing-page signals, and recent
changes. Failed or ambiguous responses are never reported as published or
recorded.

### BMW-only API worker

The Constellation fork includes a bounded OpenAI Responses API canary runner.
It is deliberately separate from the interactive ChatGPT OAuth service and is
intended to run as a Cloud Run Job with `Dockerfile.worker`. The runner exposes
only the following remote MCP tools to the model:

- due-enrollment queue lookup;
- Google Ads metadata and read-only search;
- deterministic customer-level scorecard collection and publication;
- recommendation publication; and
- enrollment-run recording.

No Google Ads mutation tool is mounted or allowed. The worker fails unless the
due account is BMW of Morristown (`4357201747`), records exactly ten review
areas, validates portal confirmations from the actual MCP calls, and uses
`counts_as_new_recommendation` rather than publication acceptance when
recording the recommendation count. The scorecard collector owns the six fixed
date windows and their aggregation server-side; the model cannot supply or
rename period keys, or publish a free-form scorecard payload.

Date-segmented performance searches explicitly exclude
`metrics.conversion_last_conversion_date`, which Google Ads does not support
with `segments.date`. When conversion recency is material, the worker must use
a separate compatible search without `segments.date`. The worker audits the
actual MCP search arguments before accepting a completed run.

Deploy the service-MCP and worker as separate Cloud Run workloads from the
same source. The service-MCP uses `GOOGLE_ADS_MCP_AUTH_MODE=service_jwt` and
`ads_mcp/worker_tools_config.yaml`; the worker signs a short-lived HS256 JWT
for every Responses API request. Both workloads receive the shared signing
secret from Secret Manager. Configure:

- `OPENAI_API_KEY` on the worker only;
- `GOOGLE_ADS_MCP_SERVICE_URL` on the worker, including the `/mcp` path;
- `GOOGLE_ADS_MCP_SERVICE_JWT_SECRET` on both workloads;
- `GOOGLE_ADS_MCP_TOOLS_CONFIG=ads_mcp/worker_tools_config.yaml` on the
  service-MCP;
- `GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS=4357201747` and
  `RECOMMENDATION_CENTER_ALLOWED_CUSTOMER_IDS=4357201747` on the service-MCP;
- optional matching `GOOGLE_ADS_MCP_SERVICE_JWT_ISSUER` and
  `GOOGLE_ADS_MCP_SERVICE_JWT_AUDIENCE` values;
- `GOOGLE_ADS_SERVICE_REFRESH_TOKEN`,
  `GOOGLE_ADS_SERVICE_OAUTH_CLIENT_ID`, and
  `GOOGLE_ADS_SERVICE_OAUTH_CLIENT_SECRET` on the service-MCP only; and
- the existing Google Ads developer token, MCC login customer, portal URL,
  ingestion key, and Sites bypass token on the service-MCP.

Use a dedicated Google OAuth client for these three offline credentials; do
not reuse the interactive ChatGPT connector's OAuth client. The deployment
script stores the dedicated client id, client secret, and refresh token as
three independent Secret Manager secrets and never prints their values.

The offline Google credentials are used only by the isolated service-MCP. The
interactive `google-ads-mcp` deployment continues to use its existing Google
OAuth proxy. Do not attach a Cloud Scheduler trigger until a manual BMW run
passes the scorecard, semantic-deduplication, ten-area coverage, and exact-once
run-recording gates.

### Configuring and Namespacing Tools

The Google Ads MCP server uses the `tools_config.yaml` to let you selectively enable or disable individual tools or tool categories (namespaces) and customize their namespace prefixes.

A default `tools_config.yaml` with all tools enabled is bundled with the package, so the server works out of the box with no extra setup. To customize your installation, the server resolves the configuration in the following order:

1. An explicit path set via the `GOOGLE_ADS_MCP_TOOLS_CONFIG` environment variable.
2. A `tools_config.yaml` file in the current working directory.
3. The default `tools_config.yaml` bundled with the package.

If an explicitly requested configuration file (via the environment variable) is missing, or any resolved file is invalid, the server raises an error and fails to start.

#### Configuration Example:
```yaml
namespaces:
  # Option 1: Enable category 'customers' with default prefix -> "customers_list_accessible_customers"
  customers: true

  # Option 2: Enable category 'search' with a custom prefix -> "query_search"
  search: "query"

  # Option 3: Fine-grained control over tools in a category
  metadata:
    enabled: true
    prefix: "metadata"
    enabled_tools:
      - get_resource_metadata: true
```


### Resources available

- `discovery-document`: Retrieve the Google Ads API discovery document. Provides the discovery document for the latest version of the Google Ads API, which describes the API surface, including resources, methods, and schemas. Host LLMs should access this resource to understand the structure of the Google Ads API and discover available features.
- `metrics`: Retrieve information about the metrics available for reporting in the Google Ads API.
- `segments`: Retrieve information about the segments available for reporting in the Google Ads API.
- `release-notes`: Retrieve the release notes for the latest version of the Google Ads API.

## Notes

1.  The MCP Server will expose your data to the Agent or LLM that you connect to it.
1.  If you have technical issues, please use the [GitHub issue tracker](https://github.com/googleads/google-ads-mcp/issues).
1.  To help us collect usage data, you will notice an extra header has been added to your API calls: this data is used to improve the product.

## Setup instructions

Setup involves the following steps:

1.  Configure Python.
1.  Configure Developer Token.
1.  Enable APIs in your project
1.  Configure Credentials.
1.  Configure your MCP client.

### Configure Python

[Install pipx](https://pipx.pypa.io/stable/#install-pipx).

### Configure Developer Token

Follow the instructions for [Obtaining a Developer Token](https://developers.google.com/google-ads/api/docs/get-started/dev-token).

Your developer token must have at least [Explorer access](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) to query production accounts. New tokens may be automatically upgraded to Explorer access; if not, you can apply through the API Center. See the [access levels documentation](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) for details.

If you see the error *"The developer token is only approved for use with test
accounts"*, your token does not yet have access to production accounts. See the
[access levels documentation](https://developers.google.com/google-ads/api/docs/access-levels)
for how to request the access level you need.

### Enable APIs in your project

[Follow the instructions](https://support.google.com/googleapi/answer/6158841)
to enable the following APIs in your Google Cloud project:

* [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com)

### Configure Credentials
#### Option 1: Using FastMCP OAuth Proxy

The server supports FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication. This is useful when running the server as a web service.

To enable it, set the following environment variables:

- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: Your Google Cloud OAuth 2.0 Client ID.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: Your Google Cloud OAuth 2.0 Client Secret.
- `GOOGLE_ADS_MCP_BASE_URL`: (Optional) The base URL where the server is accessible (defaults to `http://localhost:8080`).
- `GOOGLE_ADS_MCP_JWT_SIGNING_KEY`: (Optional) Secret key used to sign FastMCP JWT tokens across multiple server instances or deployments.
- `GOOGLE_ADS_MCP_STORAGE_TYPE`: (Optional) Storage backend for OAuth state (`filetree`, `redis`, or `memory`).
- `GOOGLE_ADS_MCP_STORAGE_PATH`: (Optional) Directory path for `filetree` persistent storage.
- `GOOGLE_ADS_MCP_STORAGE_REDIS_URL`: (Optional) Redis URL for `redis` persistent storage.
- `GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY`: (Optional) Encryption key for stored OAuth tokens.
- `GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION`: (Optional) Set to `true` to disable token encryption.

Once this is enabled, you can authenticate to the API through your MCP client.

When these variables are set, the server automatically switches to the `streamable-http` transport (SSE/HTTP) instead of `stdio`.

You will need to run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8080/mcp`).

#### Option 2: Configure credentials using Application Default Credentials

Configure your [Application Default Credentials
(ADC)](https://cloud.google.com/docs/authentication/provide-credentials-adc).
Make sure the credentials are for a user with access to your Google Ads
accounts or properties.

Credentials must include the Google Ads API scope:

```
https://www.googleapis.com/auth/adwords
```

Check out
[Manage OAuth Clients](https://support.google.com/cloud/answer/15549257)
for how to create an OAuth client.

Here are some sample `gcloud` commands you might find useful:


- Set up ADC using user credentials and an OAuth desktop or web client after
  downloading the client JSON to `YOUR_CLIENT_JSON_FILE`.

  ```shell
  gcloud auth application-default login \
    --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
    --client-id-file=YOUR_CLIENT_JSON_FILE
  ```

- Set up ADC using service account impersonation.

  ```shell
  gcloud auth application-default login \
    --impersonate-service-account=SERVICE_ACCOUNT_EMAIL \
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
  ```

When the `gcloud auth application-default` command completes, copy the
`PATH_TO_CREDENTIALS_JSON` file location printed to the console in the
following message. You will need this for a later step!

```
Credentials saved to file: [PATH_TO_CREDENTIALS_JSON]
```

#### Option 3: Configure credentials using the Google Ads API Python client library.

[Follow the instructions](https://developers.google.com/google-ads/api/docs/client-libs/python/)
to setup and configure the Google Ads API Python client library

If you have already done this and have a working `google-ads.yaml` , you can reuse this file!

In the utils.py file, change get_googleads_client() to use the load_from_storage() method.

### Configure your MCP client

Add the server to your MCP client's configuration. Below are examples for
popular clients.

#### Antigravity CLI / Antigravity Code Assist

1.  Install [Antigravity CLI](https://antigravity.google/product/antigravity-cli) or Antigravity Code Assist.

1.  Configure your server. Refer to the docs at [https://antigravity.google/docs/mcp](https://antigravity.google/docs/mcp) for details on setting up MCP servers.

- Option 1: Using FastMCP OAuth Proxy (Streamable HTTP)

  You can run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8080/mcp`).
  This also allows using FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "httpUrl":"http://localhost:8080/mcp",
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"                        
          }
        }
      }
    }
    ```

- Option 2: the Application Default Credentials method

    Replace `PATH_TO_CREDENTIALS_JSON` with the path you copied in the previous
    step.

    We also recommend that you add a `GOOGLE_CLOUD_PROJECT` attribute to the
    `env` object. Replace `YOUR_PROJECT_ID` in the following example with the
    [project ID](https://support.google.com/googleapi/answer/7014113) of your
    Google Cloud project.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

- Option 3: the Python client library method

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

#### Login Customer Id

If your access to the customer account is through a manager account, you will
need to add the customer ID of the manager account to the settings file.

See [here](https://developers.google.com/google-ads/api/docs/concepts/call-structure#cid) for details.

The final file will look like this:

  ```json
  {
    "mcpServers": {
      "google-ads-mcp": {
        "command": "pipx",
        "args": [
          "run",
          "--spec",
          "git+https://github.com/googleads/google-ads-mcp.git",
          "google-ads-mcp"
        ],
        "env": {
          "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
          "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
          "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
          "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
        }
      }
    }
  }
  ```

#### Other MCP clients (Claude Code, Cursor, VS Code, etc.)

The `mcpServers` block format is the same across all MCP clients. Add the configuration shown above to the appropriate settings file for your client (e.g., `~/.claude/settings.json` for Claude Code, `.cursor/mcp.json` for Cursor, `.vscode/mcp.json` for VS Code with Copilot).

## Deployment to Google Cloud Platform

Instead of hosting this MCP server locally, you can host it on Google Cloud Run or on any other cloud-based infrastructure. This is useful if you want to share the server across different agents or run it as a web service.

Note that this only supports authentication with an OAuth Client ID and Client Secret pair through the OAuth proxy (Option #1 above).

### Prerequisites

1.  A Google Cloud project.
2.  The `gcloud` CLI installed, authenticated, and active project set.
    ```shell
    gcloud config set project YOUR_PROJECT_ID
    ```

### Step 1: Build and Push Docker Image

You can use Cloud Build to build and push the image to Artifact Registry without needing Docker installed locally.

1.  Create a repository in Artifact Registry:
    ```shell
    gcloud artifacts repositories create mcp-servers --repository-format=docker --location=us-central1
    ```
2.  Build and submit the image:
    ```shell
    gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest .
    ```
    Replace `YOUR_PROJECT_ID` with your Google Cloud project ID.

### Step 2: Deploy to Google Cloud Run

Make sure to set the required environment variables:

- `GOOGLE_PROJECT_ID`: Your Google Cloud project ID.
- `GOOGLE_ADS_DEVELOPER_TOKEN`: The developer token you want the MCP server to use (see above).
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: The OAuth Client ID you want the MCP server to use.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: The OAuth Client secret you want the MCP server to use.
- `GOOGLE_ADS_MCP_BASE_URL`: The base URL where your MCP server is accessible: this will be automatically assigned by Google Cloud Run after your first deployment. You can update the environment variables after deployment. 
- `GOOGLE_ADS_MCP_JWT_SIGNING_KEY`: (Recommended for production) Persistent JWT signing key across Cloud Run instances.
- `GOOGLE_ADS_MCP_STORAGE_TYPE` / `GOOGLE_ADS_MCP_STORAGE_REDIS_URL`: (Recommended for production) Storage backend (e.g. `redis`) to persist OAuth tokens across instances.
- `FASTMCP_HOST`: Set this to `0.0.0.0` to allow FastMCP to accept connections from all IP addresses.

```shell
gcloud run deploy google-ads-mcp \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_PROJECT_ID=YOUR_PROJECT_ID,GOOGLE_ADS_DEVELOPER_TOKEN=YOUR_DEVELOPER_TOKEN,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=YOUR_CLIENT_ID,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=YOUR_CLIENT_SECRET,GOOGLE_ADS_MCP_BASE_URL=YOUR_BASE_URL,GOOGLE_ADS_MCP_JWT_SIGNING_KEY=YOUR_JWT_SIGNING_KEY,FASTMCP_HOST=0.0.0.0"
```

### Step 3: Configure MCP Client

Once deployed, update your MCP client configuration (refer to the docs at [https://antigravity.google/docs/mcp](https://antigravity.google/docs/mcp)) to use the Cloud Run URL.

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "httpUrl": "https://your-cloud-run-url.a.run.app/mcp"
    }
  }
}
```

## Try it out

Launch your MCP client. You should see `google-ads-mcp` listed in the
available servers.

Here are some sample prompts to get you started:

- Ask what the server can do:

  ```
  what can the ads-mcp server do?
  ```

- Ask about customers:

  ```
  what customers do I have access to?
  ```

- Ask about campaigns 

  ```
  How many active campaigns do I have?
  ```

  ```
  How is my campaign performance this week?
  ```

### Note about Customer ID

Your agent will need and ask for a customer id for most prompts. If you are 
moving between multiple customers, including the customer ID in the prompt may
be simpler.

```
How many active campaigns do I have for customer id 1234567890
```

## Contributing

Contributions welcome! See the [Contributing Guide](CONTRIBUTING.md).
