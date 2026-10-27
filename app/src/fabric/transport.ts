// Answers the SaaS UI's requests from what the scan notebook stored.
//
// The UI asks `GET /lineage/catalog`, `/lineage/nodes/<id>` … exactly as it
// asks the SaaS backend; here each request becomes one keyed read of
// dbo.ui_chunk through the connector, unpacked back into the same JSON.
import { ApiError, type Transport } from "../ui/api/client";
import { getClient } from "./client";
import { key, unpack } from "./payloads";

const NOT_STORED =
  "Not in the last scan. Run the FabriBuddy notebook again, or this view isn't available in the in-tenant edition yet.";

// An answer the backend gave as an error (core: export.ERROR_KEY): raised as
// the same ApiError, so a page behaves as it would against the SaaS.
const ERROR_KEY = "__fabribuddyError";

function answer(value: unknown): unknown {
  if (value && typeof value === "object" && ERROR_KEY in value) {
    const { [ERROR_KEY]: status, detail } = value as Record<string, unknown>;
    throw new ApiError(typeof detail === "string" ? detail : NOT_STORED, Number(status) || 500);
  }
  return value;
}

// A page often asks for the same answer twice (and a remount asks again).
const cache = new Map<string, Promise<unknown>>();

async function readChunks(k: string): Promise<string[]> {
  const rows = await getClient()
    .connectors.fabribuddy.UiChunk.select(["part", "body"])
    .where({ k: { eq: k } })
    .orderBy({ part: "asc" })
    .first(100000)
    .execute();
  return rows.map((row) => row.body);
}

export const fabricTransport: Transport = (path) => {
  let pending = cache.get(path);
  if (!pending) {
    pending = (async () => {
      const chunks = await readChunks(await key(path));
      if (chunks.length === 0) throw new ApiError(NOT_STORED, 404);
      return answer(await unpack(chunks));
    })();
    // A failure is not cached: the next visit asks again.
    pending.catch(() => cache.delete(path));
    cache.set(path, pending);
  }
  return pending;
};

/**
 * `npm run dev:preview`: the same answers from public/local-chunks.json (a
 * {key: chunks} map written by `core/tools/export_local.py`), no Fabric.
 */
export function localTransport(): Transport {
  let all: Promise<Record<string, string[]>> | null = null;
  return async (path) => {
    all ??= fetch("/local-chunks.json").then((r) => r.json());
    const chunks = (await all)[await key(path)];
    if (!chunks) throw new ApiError(NOT_STORED, 404);
    return answer(await unpack(chunks));
  };
}
