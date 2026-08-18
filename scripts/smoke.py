"""End-to-end smoke test of the policy engine and journal."""
import tempfile, os
from blackout_core import *

reg = ToolRegistry()

@reg.tool(
    effect=Effect.READ, min_tier=Tier.RULES, offline_policy=OfflinePolicy.EXECUTE,
    tier_descriptions={1: "Read the current inventory level for a SKU.", 2: "Check stock."},
)
def read_inventory(sku): return {"sku": sku, "level": 4}

@reg.tool(
    effect=Effect.EXTERNAL_WRITE, min_tier=Tier.CLOUD, offline_policy=OfflinePolicy.DEFER,
    idempotency_key=lambda a: f"restock:{a['sku']}:{a['window']}",
    preconditions=["inventory.below_threshold"],
    max_precondition_staleness_s=3600, ttl_seconds=14400, reversible=False,
)
def place_restock_order(sku, qty, window): ...

@reg.tool(
    effect=Effect.EXTERNAL_WRITE, min_tier=Tier.CLOUD, offline_policy=OfflinePolicy.REFUSE,
    idempotency_key=lambda a: f"page:{a['oncall']}",
)
def page_oncall(oncall): ...

eng = PolicyEngine(reg)
fresh = [PreconditionValue("inventory.below_threshold", {"level": 4, "threshold": 10}, 12.0, True)]
stale = [PreconditionValue("inventory.below_threshold", {"level": 4, "threshold": 10}, 7200.0, True)]
unmet = [PreconditionValue("inventory.below_threshold", {"level": 40, "threshold": 10}, 5.0, False)]
args = {"sku": "SKU-991", "qty": 40, "window": "2026-W34"}

cases = [
    ("read @ tier2",          "read_inventory", Tier.LOCAL, {"sku":"S"}, ()),
    ("restock @ tier1",       "place_restock_order", Tier.CLOUD, args, fresh),
    ("restock @ tier2 fresh", "place_restock_order", Tier.LOCAL, args, fresh),
    ("restock @ tier2 stale", "place_restock_order", Tier.LOCAL, args, stale),
    ("restock @ tier2 unmet", "place_restock_order", Tier.LOCAL, args, unmet),
    ("page @ tier2",          "page_oncall", Tier.LOCAL, {"oncall":"sre"}, ()),
    ("restock @ tier0",       "place_restock_order", Tier.JOURNAL_DOWN, args, fresh),
    ("read @ tier0",          "read_inventory", Tier.JOURNAL_DOWN, {"sku":"S"}, ()),
]
print("POLICY ENGINE")
for label, tool, tier, a, pc in cases:
    r = eng.evaluate(tool, tier, a, pc)
    print(f"  {label:24s} -> {r.decision.value:8s} [{r.reason}] {r.detail}")

print("\nTIER RESOLVER")
tr = TierResolver(promote_after=3)
for ok in (True, True, True):
    tr.record_probe(ok, 0.2)
print(f"  after 3 good probes      -> tier {int(tr.tier)}")
tr.record_probe(False)
print(f"  after 1 failure          -> tier {int(tr.tier)}")
tr.record_probe(True, 0.1); tr.record_probe(True, 0.1)
print(f"  after 2 good (needs 3)   -> tier {int(tr.tier)}")
tr.set_journal_available(False)
print(f"  journal down             -> tier {int(tr.tier)}")
tr.set_journal_available(True); tr.set_local_model_available(False)
print(f"  local model down         -> tier {int(tr.tier)}")

print("\nJOURNAL")
path = os.path.join(tempfile.mkdtemp(), "j.db")
with IntentJournal(path, secret=b"k"*32) as j:
    key = eng.idempotency_key("place_restock_order", args)
    i1 = j.append(Intent.from_evaluation("place_restock_order", args, key, 2, 14400, fresh,
                                         task_checkpoint_id="ckpt_1"))
    i2 = j.append(Intent.from_evaluation("place_restock_order", args, key, 2, 14400, fresh,
                                         task_checkpoint_id="ckpt_gone"))
    print(f"  appended 2, same idem key: {key}")
    print(f"  pending                  -> {len(j.pending())}")
    print(f"  duplicates of key        -> {len(j.duplicates_of(key))}")
    print(f"  max_source_age_s         -> {i1.max_source_age_s}")
    orph = j.orphan_missing_checkpoints(["ckpt_1"])
    print(f"  orphaned (ckpt lost)     -> {[o.id[:12] for o in orph]}")
    print(f"  pending after orphan     -> {len(j.pending())}")
    j.resolve(i1.id, IntentStatus.REJECTED, "stock replenished")
    print(f"  rejected                 -> {len(j.by_status(IntentStatus.REJECTED))}")

    expiring = j.append(Intent.from_evaluation("place_restock_order", args, "k2", 2, 0, fresh))
    print(f"  ttl=0 intent expired     -> {expiring.is_expired()}")
    print(f"  pending sweeps expired   -> {len(j.pending())}")

print("\n  tamper check:")
import sqlite3
c = sqlite3.connect(path); c.execute("UPDATE intents SET args = '{\"sku\":\"HACKED\"}'"); c.commit(); c.close()
with IntentJournal(path, secret=b"k"*32) as j:
    hit = j.verify_all()
    print(f"    verify_all quarantined -> {len(hit)} record(s)")
    print(f"    pending after tamper   -> {len(j.pending())}")
    c = j.corrupt_records()
    print(f"    quarantined as corrupt -> {len(c)}")
    print(f"    still visible for review-> {[r['tool'] for r in c]}")
