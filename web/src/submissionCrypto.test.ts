import {
  constants,
  createDecipheriv,
  generateKeyPairSync,
  privateDecrypt,
  type KeyObject,
  webcrypto,
} from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { encryptSubmission, wrapEncryptedSubmission } from "./submissionCrypto";

describe("submission encryption", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates a forge fallback envelope compatible with RSA-OAEP and AES-GCM", async () => {
    const { publicKey, privateKey } = generateKeyPairSync("rsa", {
      modulusLength: 3072,
      publicExponent: 0x10001,
    });
    const publicKeySpki = publicKey.export({ type: "spki", format: "der" }).toString("base64url");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      algorithm: "RSA-OAEP-256+A256GCM",
      key_version: "test-v1",
      public_key_spki: publicKeySpki,
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    vi.stubGlobal("crypto", {
      getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
    } as unknown as Crypto);

    const purpose = "tenant:tenant-1:exchange_account_create" as const;
    const payload = { api_key: "test-api-key-value", api_secret: "test-api-secret-value" };
    const envelope = await encryptSubmission(purpose, payload);
    expect(decryptEnvelope(privateKey, envelope)).toEqual({
      purpose,
      request_id: envelope.request_id,
      issued_at: envelope.issued_at,
      payload,
    });
  });

  it.each(["tenant:tenant-1:telegram_bot_config", "user:owner-user:reauthenticate", "tenant:tenant-1:user_create"] as const)("creates a native WebCrypto envelope compatible with server primitives: %s", async purpose => {
    const { publicKey, privateKey } = generateKeyPairSync("rsa", {
      modulusLength: 3072,
      publicExponent: 0x10001,
    });
    const publicKeySpki = publicKey.export({ type: "spki", format: "der" }).toString("base64url");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      algorithm: "RSA-OAEP-256+A256GCM",
      key_version: "test-v1",
      public_key_spki: publicKeySpki,
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    vi.stubGlobal("crypto", webcrypto as unknown as Crypto);

    const payload = purpose.endsWith(":user_create") ? { username: "new-user", initial_password: "password-test-fixture", role: "VIEWER" } : purpose.startsWith("user:") ? { password: "password-test-fixture" } : { bot_token: "test-bot-token-value", allowed_user_ids: [123456] };
    const envelope = await encryptSubmission(purpose, payload);

    expect(decryptEnvelope(privateKey, envelope)).toEqual({
      purpose,
      request_id: envelope.request_id,
      issued_at: envelope.issued_at,
      payload,
    });
    expect(wrapEncryptedSubmission(envelope)).toEqual({ encrypted_submission: envelope });
  });
});

function decryptEnvelope(
  privateKey: KeyObject,
  envelope: Awaited<ReturnType<typeof encryptSubmission>>,
): unknown {
  const aad = Buffer.from(
    `gatescope-submission-v1\n${envelope.key_version}\n${envelope.purpose}\n${envelope.request_id}\n${envelope.issued_at}`,
    "utf8",
  );
  const dataKey = privateDecrypt({
    key: privateKey,
    padding: constants.RSA_PKCS1_OAEP_PADDING,
    oaepHash: "sha256",
    oaepLabel: aad,
  }, Buffer.from(envelope.wrapped_key, "base64url"));
  const encrypted = Buffer.from(envelope.ciphertext, "base64url");
  const decipher = createDecipheriv(
    "aes-256-gcm", dataKey, Buffer.from(envelope.nonce, "base64url"),
  );
  decipher.setAAD(aad);
  decipher.setAuthTag(encrypted.subarray(-16));
  const plaintext = Buffer.concat([
    decipher.update(encrypted.subarray(0, -16)),
    decipher.final(),
  ]);
  return JSON.parse(plaintext.toString("utf8"));
}
