# Repository scope and setup

Sanitized source for a per-user AI usage and estimated-cost dashboard. This is a
portfolio project, not a hosted service or a self-provisioning application.

The design extends the
[AI gateway billing sample](https://github.com/tjsullivan1/tjs-scripts/tree/master/solutions/ai-gateway-billing-sample)
with delegated-user attribution and a Fabric-backed analytics path. Preserve
upstream attribution and applicable licenses. No new license granting rights to
third-party material is supplied.

## Configure your own environment

1. Create your own ignored `.deployment` directory and copy
   [config/azure.example.json](config/azure.example.json) to
   `.deployment/azure.json`. Replace every placeholder and approve the viewer list.
   The example does not provision infrastructure.
2. Copy
   [deployment.example.ts](rayfin/functions/src/deployment.example.ts) to the
   ignored `deployment.ts` beside it. Configure a private snapshot URL and approved
   viewers. An empty viewer list denies access.
3. Supply the Rayfin client/environment metadata in your own tenant. Generated
   secret/client/deployment files are deliberately absent.
4. Configure Azure API Management, Foundry, Log Analytics, Fabric connections,
   and required permissions before using the live-data scripts.

## Dependencies and limitations

Rayfin SDK dependencies require appropriate package access. Internal package-feed
URLs were removed; a fresh installation from a public registry has not been
verified and may not be available. Do not add registry tokens or private feed
configuration to this repository. No SDK package binaries or private package
sources are redistributed here.

The source README describes the original architecture and historical connector
verification. This sanitized copy has no live connections. GitHub usage and Fabric
data-agent usage connectors are not implemented.

## Privacy defaults

- HTTP request/response body capture, LLM message logging, and client-IP logging
  are disabled. The usage trace does not extract prompt or completion content.
- The transform drops legacy content/IP columns before writing curated events.
  Existing remote logs are not deleted by these source changes.
- The M365 collector requests reports-read permission only; it does not change
  tenant-wide report-name concealment.
- Per-user identity and usage metadata still require approved access, retention,
  notice, and governance. Estimated cost is not an invoice.

## Verification

Twelve Python synthetic/privacy tests and eleven TypeScript analytics/contract
tests passed. The dashboard also passed TypeScript checking with existing local
SDK dependencies through an isolated validation configuration.

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
node --test tests/analytics.test.ts tests/contract.test.ts
```

These are offline checks, not verification of public package availability, a
fresh full build, deployment, or live authentication. Use a Node version supporting
the project's TypeScript test execution and configure your own environment before
attempting a live demonstration.
