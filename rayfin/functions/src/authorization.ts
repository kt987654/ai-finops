export function assertViewerClaims(claims: unknown, viewerEmails: readonly string[]): void {
  // The Fabric function host verifies ctx.accessToken before injecting it.
  // This is an additional allowlist check, not a standalone JWT verifier.
  let email: unknown;
  if (claims !== null && typeof claims === "object") {
    if ("email" in claims) email = claims.email;
    else if ("xms_attr" in claims && claims.xms_attr !== null && typeof claims.xms_attr === "object") {
      const attributes: unknown[] = Object.values(claims.xms_attr);
      if (attributes.length === 1 && attributes[0] !== null && typeof attributes[0] === "object"
        && "rfn_email" in attributes[0]) email = attributes[0].rfn_email;
    }
  }
  const allowed = new Set(viewerEmails.map((value) => value.toLowerCase()));
  if (claims === null || typeof claims !== "object"
    || !("sub" in claims) || typeof claims.sub !== "string" || claims.sub.length === 0
    || typeof email !== "string" || !allowed.has(email.toLowerCase())) {
    throw new Error("This enterprise usage dashboard is available only to approved viewers.");
  }
}
