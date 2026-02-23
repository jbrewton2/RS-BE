param(
  [string]$TargetPath = ".\core\dynamo_meta.py"
)

$ErrorActionPreference="Stop"
cd "C:\Users\JoshBrewton\Desktop\CSS\css-backend"

if (!(Test-Path $TargetPath)) { throw "Missing: $TargetPath" }

$src = Get-Content -Raw -LiteralPath $TargetPath

# 1) Ensure Decimal import exists (needed for Dynamo float safety)
if ($src -notmatch '(?m)^\s*from\s+decimal\s+import\s+Decimal\b') {
  # insert after typing imports (best-effort)
  if ($src -match '(?m)^(from\s+typing\s+import\s+.+)$') {
    $src = [regex]::Replace($src, '(?m)^(from\s+typing\s+import\s+.+)$', "`$1`r`nfrom decimal import Decimal", 1)
  } else {
    # fallback: top of file after __future__
    $src = [regex]::Replace($src, '(?m)^(from __future__ import .+\r?\n)', "`$1from decimal import Decimal`r`n", 1)
  }
}

# 2) Ensure a helper exists to convert floats -> Decimal recursively
if ($src -notmatch '(?m)^\s*def\s+_dynamo_safe\(') {
  $helper = @"
def _dynamo_safe(value):
    \"\"\"Recursively convert floats to Decimal for DynamoDB serialization.\"\"\"
    if value is None:
        return None
    if isinstance(value, float):
        # Convert via string to preserve intended value without binary float artifacts
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(v) for v in value]
    return value

"@
  # place helper right after class DynamoMeta: line (module-level helpers live above class usually)
  # best effort: insert before "class DynamoMeta:"
  if ($src -match '(?m)^class\s+DynamoMeta:') {
    $src = [regex]::Replace($src, '(?m)^class\s+DynamoMeta:', $helper + 'class DynamoMeta:', 1)
  } else {
    # fallback: append near top (after imports block)
    $src = $helper + $src
  }
}

# 3) Replace the entire injected AI persistence block to fix indentation + floats
# Anchor from the comment to just before the next add("pdf_s3_key"... line
$pattern = '(?s)\n\s*# Persist AI analysis outputs \(UI contract\).*?\n\s*add\("rag",\s*rag_compact\)\s*\n'
$m = [regex]::Match($src, $pattern)
if (-not $m.Success) {
  throw "Could not locate the AI persistence block to replace (pattern drifted). Search for: 'Persist AI analysis outputs (UI contract)'"
}

$replacement = @"
            # Persist AI analysis outputs (UI contract)
            # NOTE: Dynamo item size is limited (~400KB). Keep payload bounded.
            last_analysis_at = review.get("lastAnalysisAt") or review.get("last_analysis_at") or None
            if isinstance(last_analysis_at, str) and last_analysis_at.strip():
                add("lastAnalysisAt", last_analysis_at.strip())

            ai_summary = review.get("aiSummary")
            if isinstance(ai_summary, str) and ai_summary.strip():
                add("aiSummary", ai_summary.strip()[:50000])

            ai_risks = review.get("aiRisks")
            if isinstance(ai_risks, list) and ai_risks:
                # Persist a compact risk list (and Dynamo-safe floats)
                compact_risks = []
                for r in ai_risks[:200]:
                    if not isinstance(r, dict):
                        continue
                    compact_risks.append({
                        "id": r.get("id"),
                        "label": r.get("label"),
                        "category": r.get("category"),
                        "severity": r.get("severity"),
                        "scope": r.get("scope"),
                        "document_name": r.get("document_name"),
                        "rationale": (str(r.get("rationale") or "")[:2000] if r.get("rationale") is not None else None),
                        "related_flags": (r.get("related_flags")[:50] if isinstance(r.get("related_flags"), list) else None),
                        # strip heavy evidence blobs from persisted risks (UI can rebuild from rag if needed)
                        "evidence": [],
                    })
                add("aiRisks", _dynamo_safe(compact_risks))

            rag = review.get("rag")
            if isinstance(rag, dict):
                # Store a compact RAG blob only (Dynamo-safe floats)
                rag_compact = {}

                s = rag.get("summary")
                if isinstance(s, str) and s.strip():
                    rag_compact["summary"] = s.strip()[:50000]

                rc = rag.get("retrieved_counts")
                if isinstance(rc, dict):
                    rag_compact["retrieved_counts"] = rc

                w = rag.get("warnings")
                if isinstance(w, list):
                    rag_compact["warnings"] = w[:50]

                st = rag.get("stats")
                if isinstance(st, dict):
                    rag_compact["stats"] = st

                # sections can be huge; keep bounded + truncate evidence text
                secs = rag.get("sections")
                if isinstance(secs, list):
                    safe_secs = []
                    for sec in secs[:30]:
                        if not isinstance(sec, dict):
                            continue

                        evs_in = sec.get("evidence") or []
                        safe_evs = []
                        if isinstance(evs_in, list):
                            for ev in evs_in[:10]:
                                if not isinstance(ev, dict):
                                    continue
                                safe_evs.append({
                                    "docId": ev.get("docId"),
                                    "doc": ev.get("doc"),
                                    "charStart": ev.get("charStart"),
                                    "charEnd": ev.get("charEnd"),
                                    # score is often a float -> will be Decimal via _dynamo_safe
                                    "score": ev.get("score"),
                                    "text": (str(ev.get("text") or "")[:800] if ev.get("text") is not None else None),
                                })

                        safe_secs.append({
                            "id": sec.get("id"),
                            "title": sec.get("title"),
                            "owner": sec.get("owner"),
                            "findings": (sec.get("findings") or [])[:10] if isinstance(sec.get("findings"), list) else sec.get("findings"),
                            "gaps": (sec.get("gaps") or [])[:10] if isinstance(sec.get("gaps"), list) else sec.get("gaps"),
                            "recommended_actions": (sec.get("recommended_actions") or [])[:10]
                                if isinstance(sec.get("recommended_actions"), list)
                                else sec.get("recommended_actions"),
                            "evidence": safe_evs,
                        })

                    rag_compact["sections"] = safe_secs

                add("rag", _dynamo_safe(rag_compact))

"@

$updated = [regex]::Replace($src, $pattern, "`n" + $replacement.TrimEnd() + "`n", 1)
if ($updated -eq $src) { throw "No changes applied; replace failed unexpectedly." }

$backup = "$TargetPath.bak.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Copy-Item -LiteralPath $TargetPath -Destination $backup -Force

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Resolve-Path $TargetPath), $updated, $utf8NoBom)

Write-Host "Patched: $TargetPath"
Write-Host "Backup:  $backup"
