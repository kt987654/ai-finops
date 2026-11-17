import { UserDataFunctions, AudienceType, type RayfinContext } from "@microsoft/fabric-user-data-functions";
import { assertViewerClaims } from "./authorization.js";
import { parseSnapshot, type EnterpriseSnapshot } from "./contract.js";
import { deployment } from "./deployment.js";

const udf = new UserDataFunctions();

udf.func("getEnterpriseUsage", async (
  ctx: RayfinContext<Record<string, never>, AudienceType.Storage>,
): Promise<EnterpriseSnapshot> => {
  try {
    const encodedClaims = ctx.accessToken.split(".")[1];
    if (!encodedClaims) throw new Error("Missing authenticated caller claims.");
    const claims: unknown = JSON.parse(Buffer.from(encodedClaims, "base64url").toString("utf8"));
    assertViewerClaims(claims, deployment.viewerEmails);
    const response = await fetch(deployment.snapshotUrl, {
      headers: { Authorization: "Bearer " + ctx.Tokens.Storage, "Cache-Control": "no-cache" },
      signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) {
      throw new Error(`Fabric snapshot read failed (HTTP ${response.status}). Check the ingestion pipeline and app-owner access.`);
    }
    const data: unknown = await response.json();
    return parseSnapshot(data);
  } catch (error) {
    console.error("getEnterpriseUsage failed", error instanceof Error ? error.message : "Unknown failure");
    throw error;
  }
}, []);
