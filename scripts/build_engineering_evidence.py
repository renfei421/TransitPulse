"""Render a public, aggregate-only engineering evidence page from verified artifacts."""
import html
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    read = lambda name: json.loads((root / "docs/evidence" / name).read_text(encoding="utf-8"))
    archive = read("archive-restore-verification.json")
    analysis = read("offline-analysis-verification.json")
    parity = read("local-replay-api-parity.json")
    state = read("cloud-retirement.json")
    billing = "Cloud resources removed" if state["billing_stopped_verified"] else "Cloud retirement pending account access / backup"
    content = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TransitPulse — Engineering evidence</title><style>
*{box-sizing:border-box}body{margin:0;background:#eef3f5;color:#20343e;font:16px/1.65 system-ui,Segoe UI,sans-serif}header{background:#172f3b;color:white;padding:24px 5%;font-weight:700;letter-spacing:2px}main{max-width:1280px;margin:30px auto;padding:0 28px}h1{font-size:35px;line-height:1.2;margin:8px 0 12px}h2{font-size:19px;margin:0 0 15px}.eyebrow{font-size:12px;color:#3268aa;font-weight:700;letter-spacing:2px}.sub{color:#657986}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:28px 0}.card,section{background:white;border:1px solid #d9e4e8;border-radius:12px;padding:22px}.value{font-size:30px;font-weight:700;color:#245fa6}.label{font-size:13px;color:#657986}.grid{display:grid;grid-template-columns:1.15fr 1fr;gap:20px}ol{padding-left:22px}li{margin:13px 0}.flow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:15px 0}.step{padding:12px;border-radius:8px;background:#eef5fa;font-size:13px}.note{background:#fff7e7;padding:15px;border-radius:8px;font-size:13px;margin:20px 0}.ok{color:#247761;font-weight:650}table{width:100%;border-collapse:collapse;font-size:14px}td{border-bottom:1px solid #e5ecef;padding:9px 0}footer{color:#657986;font-size:12px;margin:20px 0}@media(max-width:800px){.metrics{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}}
</style><header>TRANSITPULSE / ENGINEERING EVIDENCE</header><main><div class="eyebrow">FROZEN EXPERIMENT · VERIFIED LOCAL RECOVERY</div><h1>From paid cloud execution<br>to reproducible local evidence.</h1><div class="sub">Kubernetes / Fission / Elasticsearch · External Jev API · Python</div>
<div class="metrics"><div class="card"><div class="value">90,576</div><div class="label">Completed model results</div></div><div class="card"><div class="value">90,587</div><div class="label">Durable request receipts</div></div><div class="card"><div class="value">US$11.732</div><div class="label">Model accounting / US$15 cap</div></div><div class="card"><div class="value">TOTAL_DOCS</div><div class="label">Documents restored and fingerprinted</div></div></div>
<div class="grid"><section><h2>Failure recovery with a budget boundary</h2><div class="flow"><span class="step">Reserve cost</span>→<span class="step">Call provider</span>→<span class="step">Save native response</span>→<span class="step">Validate & persist</span></div><ol><li>Saved native responses can be replayed without another provider call.</li><li>Uncertain failed requests retain their cost reservations.</li><li>At most three paid attempts per document, counted across restarts.</li><li>Frozen input fingerprints, model version and question contract remain unchanged.</li></ol><div class="note">This is not a cross-system exactly-once guarantee. Cost accounting is not the provider invoice and excludes cloud infrastructure.</div></section>
<section><h2>Restore acceptance</h2><table><tr><td>Full application indices</td><td class="ok">INDEX_COUNT verified</td></tr><tr><td>Document IDs + complete source</td><td class="ok">Fingerprints match</td></tr><tr><td>Cloud / local aggregate responses</td><td class="ok">PARITY_COUNT checks match</td></tr><tr><td>Offline score and contract checks</td><td class="ok">90,576 passed</td></tr><tr><td>Analysis and cost recalculation</td><td class="ok">Matches archived results</td></tr><tr><td>Human evaluation tasks</td><td>HUMAN_COUNT unlabelled</td></tr></table><div class="note">BILLING_STATUS. Backups and local replay do not by themselves stop cloud billing.</div></section></div>
<footer>Historical cloud deployment evidence and current restoration evidence are separate. No production autoscaling, calibrated accuracy or causal effect is claimed. Source evidence: docs/evidence/.</footer></main></html>'''
    for key, value in {"TOTAL_DOCS": f'{archive["total_documents"]:,}', "INDEX_COUNT": archive["index_count"],
                       "PARITY_COUNT": len(parity["checks"]), "HUMAN_COUNT": analysis["human_review"]["task_count"],
                       "BILLING_STATUS": billing}.items():
        content = content.replace(key, html.escape(str(value)))
    (root / "frontend/engineering-evidence.html").write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
