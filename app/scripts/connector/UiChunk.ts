// dbo.ui_chunk in the Fabric SQL database: every page's answer, packed by the
// scan notebook (core/src/fabribuddy_intenant/sqldb.py creates the table).
// Read-only for everyone who can open the app.
import { entity, int, text, role } from "@microsoft/rayfin-core";
import { Source } from "@microsoft/rayfin-connectors";

@role("authenticated", ["read"])
@entity()
export class UiChunk extends Source({
  schema: "dbo",
  table: "ui_chunk",
  primaryKey: ["k", "part"],
}) {
  @text({ max: 64 }) k!: string;
  @int() part!: number;
  @text({ max: 4000 }) body!: string;
}
