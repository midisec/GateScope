import { api } from "./api";

export type SubmissionPurpose =
  | "auth_login"
  | `user:${string}:reauthenticate`
  | `tenant:${string}:user_create`
  | `tenant:${string}:telegram_bot_config`
  | `tenant:${string}:coinglass_config`
  | `tenant:${string}:exchange_account_create`
  | `tenant:${string}:exchange_credential_rotate:${string}`;

export type EncryptedSubmission = {
  alg: "RSA-OAEP-256+A256GCM";
  key_version: string;
  request_id: string;
  issued_at: number;
  purpose: SubmissionPurpose;
  wrapped_key: string;
  nonce: string;
  ciphertext: string;
};

export type EncryptedSubmissionRequest = {
  encrypted_submission: EncryptedSubmission;
};

export function wrapEncryptedSubmission(
  encryptedSubmission: EncryptedSubmission,
): EncryptedSubmissionRequest {
  return { encrypted_submission: encryptedSubmission };
}

type SubmissionKeyResponse = {
  algorithm: string;
  key_version: string;
  public_key_spki: string;
};

const ALGORITHM = "RSA-OAEP-256+A256GCM" as const;

export async function encryptSubmission(
  purpose: SubmissionPurpose,
  payload: Record<string, unknown>,
): Promise<EncryptedSubmission> {
  const browserCrypto = globalThis.crypto;
  if (!browserCrypto?.getRandomValues) throw new Error("当前浏览器不支持安全随机数");
  if (!validPurpose(purpose)) throw new Error("加密提交用途无效");

  const keyResponse = await api<SubmissionKeyResponse>("/api/v2/security/submission-key");
  if (
    keyResponse.algorithm !== ALGORITHM
    || typeof keyResponse.key_version !== "string"
    || !keyResponse.key_version
    || typeof keyResponse.public_key_spki !== "string"
    || !keyResponse.public_key_spki
  ) {
    throw new Error("服务器加密配置不可用");
  }

  const requestId = uuidV4();
  const issuedAt = Date.now();
  const aad = new TextEncoder().encode(
    `gatescope-submission-v1\n${keyResponse.key_version}\n${purpose}\n${requestId}\n${issuedAt}`,
  );
  const plaintext = new TextEncoder().encode(JSON.stringify({
    purpose,
    request_id: requestId,
    issued_at: issuedAt,
    payload,
  }));
  const nonce = browserCrypto.getRandomValues(new Uint8Array(12));

  try {
    const encrypted = browserCrypto.subtle
      ? await encryptWithWebCrypto(
        browserCrypto.subtle, keyResponse.public_key_spki, plaintext, aad, nonce,
      )
      : await encryptWithForge(
        browserCrypto, keyResponse.public_key_spki, plaintext, aad, nonce,
      );
    return {
      alg: ALGORITHM,
      key_version: keyResponse.key_version,
      request_id: requestId,
      issued_at: issuedAt,
      purpose,
      wrapped_key: base64UrlEncode(encrypted.wrappedKey),
      nonce: base64UrlEncode(nonce),
      ciphertext: base64UrlEncode(encrypted.ciphertext),
    };
  } catch {
    throw new Error("浏览器加密失败");
  } finally {
    plaintext.fill(0);
    aad.fill(0);
    nonce.fill(0);
  }
}

function uuidV4(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

async function encryptWithWebCrypto(
  subtle: SubtleCrypto,
  publicKeySpki: string,
  plaintext: Uint8Array,
  aad: Uint8Array,
  nonce: Uint8Array,
): Promise<{ wrappedKey: Uint8Array; ciphertext: Uint8Array }> {
  const publicKey = await subtle.importKey(
    "spki",
    base64UrlDecode(publicKeySpki),
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"],
  );
  const aesKey = await subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt"]);
  const rawAesKey = new Uint8Array(await subtle.exportKey("raw", aesKey));
  try {
    const [wrappedKey, ciphertext] = await Promise.all([
      subtle.encrypt({ name: "RSA-OAEP", label: exactArrayBuffer(aad) }, publicKey, exactArrayBuffer(rawAesKey)),
      subtle.encrypt(
        {
          name: "AES-GCM",
          iv: exactArrayBuffer(nonce),
          additionalData: exactArrayBuffer(aad),
          tagLength: 128,
        },
        aesKey,
        exactArrayBuffer(plaintext),
      ),
    ]);
    return {
      wrappedKey: new Uint8Array(wrappedKey),
      ciphertext: new Uint8Array(ciphertext),
    };
  } finally {
    rawAesKey.fill(0);
  }
}

async function encryptWithForge(
  browserCrypto: Crypto,
  publicKeySpki: string,
  plaintext: Uint8Array,
  aad: Uint8Array,
  nonce: Uint8Array,
): Promise<{ wrappedKey: Uint8Array; ciphertext: Uint8Array }> {
  const forge = (await import("node-forge")).default;
  const rawAesKey = browserCrypto.getRandomValues(new Uint8Array(32));
  const oaepSeed = browserCrypto.getRandomValues(new Uint8Array(32));
  try {
    const publicKey = forge.pki.publicKeyFromAsn1(
      forge.asn1.fromDer(bytesToBinary(base64UrlDecode(publicKeySpki))),
    );
    const wrappedKey = publicKey.encrypt(bytesToBinary(rawAesKey), "RSA-OAEP", {
      md: forge.md.sha256.create(),
      mgf1: { md: forge.md.sha256.create() },
      label: bytesToBinary(aad),
      seed: bytesToBinary(oaepSeed),
    });
    const cipher = forge.cipher.createCipher("AES-GCM", bytesToBinary(rawAesKey));
    cipher.start({
      iv: bytesToBinary(nonce),
      additionalData: bytesToBinary(aad),
      tagLength: 128,
    });
    cipher.update(forge.util.createBuffer(bytesToBinary(plaintext), "raw"));
    if (!cipher.finish()) throw new Error("GCM encryption failed");
    const ciphertext = cipher.output.getBytes() + cipher.mode.tag.getBytes();
    return {
      wrappedKey: binaryToBytes(wrappedKey),
      ciphertext: binaryToBytes(ciphertext),
    };
  } finally {
    rawAesKey.fill(0);
    oaepSeed.fill(0);
  }
}

function validPurpose(purpose: string): boolean {
  return purpose === "auth_login" || /^user:[A-Za-z0-9_-]+:reauthenticate$/.test(purpose) || /^tenant:[A-Za-z0-9_-]+:(?:user_create|telegram_bot_config|coinglass_config|exchange_account_create|exchange_credential_rotate:[A-Za-z0-9_-]+)$/.test(purpose);
}

function base64UrlDecode(value: string): ArrayBuffer {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid base64url");
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const binary = atob(value.replace(/-/g, "+").replace(/_/g, "/") + padding);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return bytes.buffer;
}

function base64UrlEncode(bytes: Uint8Array): string {
  return btoa(bytesToBinary(bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function bytesToBinary(bytes: Uint8Array | ArrayBuffer): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (let offset = 0; offset < view.length; offset += 0x8000) {
    binary += String.fromCharCode(...view.subarray(offset, offset + 0x8000));
  }
  return binary;
}

function binaryToBytes(value: string): Uint8Array {
  return Uint8Array.from(value, (character) => character.charCodeAt(0));
}

function exactArrayBuffer(value: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(value.byteLength);
  copy.set(value);
  return copy.buffer;
}
