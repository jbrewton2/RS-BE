$ErrorActionPreference="Stop"

$path = ".\rag\service.py"
if (-not (Test-Path $path)) { throw "Missing: $path" }

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item $path "$path.bak_set_review_summary_prompt_$ts" -Force

$raw = Get-Content $path -Raw

# Target ONLY the review_summary prompt f-string body
$pattern = '(?s)(if m == "review_summary":\s*\r?\n\s*prompt = f""")(?<body>.*?)(\r?\n\s*"""\.strip\(\)\s*\r?\n\s*else:)'
$match = [regex]::Match($raw, $pattern)
if (-not $match.Success) { throw "Could not locate review_summary prompt f-string block." }

# Authoritative review_summary prompt (single copy, no duplicates)
$newBody = @"
OVERVIEW
Write ONE unified cross-document executive brief for this review.

HARD RULES (REVIEW SUMMARY MODE)
- Plain text only. No markdown.
- Use ONLY evidence from within:
  ===BEGIN CONTRACT EVIDENCE=== ... ===END CONTRACT EVIDENCE===
- Evidence MUST be copied only from within those blocks.
- Do NOT treat QUESTION lines, headings, or instructions as evidence.
- Do NOT invent. If evidence is missing for a section, write exactly:
  Insufficient evidence retrieved for this section.
  and add ONE question to GAPS / QUESTIONS FOR GOVERNMENT.
- NO bracket placeholders like "[insert ...]" or templating filler.
- If you cannot quote evidence for a claim, you MUST NOT write the claim.
- Do NOT use general knowledge. Every factual statement must be supported by an inline quote.

OUTPUT STYLE (MIXED OK)
- OVERVIEW may be a short paragraph (2-4 sentences) for clarity.
- All other sections: bullets preferred. If a short paragraph helps, keep it to 2-4 sentences max then bullets.
- Bullets should be concrete; prefer shall/must/will language when present.
- Every non-gap bullet MUST end with one short direct quote snippet from evidence in parentheses.
  Example: - Hosting must be IL5 ("...IL5...")

RISK REGISTER RULES (STRICT)
- Provide up to 8 risks.
- Each risk must be ONE LINE exactly:
  1. <Risk> | Impact (H/M/L) | Likelihood (H/M/L) | Owner | Evidence ("...") | Mitigation
- Evidence quote must be copied from within evidence blocks and be <= 12 words.
- If you cannot quote evidence for a risk, do NOT fabricate it. Use:
  1. Insufficient evidence | Impact (U) | Likelihood (U) | Owner TBD | Evidence ("Insufficient evidence retrieved") | Mitigation: Request clarification

SECTIONS (exact order)

OVERVIEW

MISSION & OBJECTIVE
SCOPE OF WORK

SECURITY, COMPLIANCE & HOSTING CONSTRAINTS
DATA / INTEGRATIONS
DELIVERABLES / TIMELINES
LEGAL / DATA RIGHTS
FINANCIAL
SUBMISSION / EVALUATION

STAKEHOLDERS & OWNERSHIP
- Security/ISSO:
- Legal/Contracts:
- Program/PM:
- Engineering/Architecture:
- Finance:
- Customer/KO/CO:

RISK REGISTER (TOP 8)

GAPS / QUESTIONS FOR GOVERNMENT

RECOMMENDED INTERNAL ACTIONS
- Route to Security, Legal/Contracts, PM, Engineering, Finance as applicable (bullets with quotes).

RETRIEVED CONTEXT
{context}
"@

# Write back: replace only the matched body group
$updated = $raw.Substring(0, $match.Groups["body"].Index) + $newBody + $raw.Substring($match.Groups["body"].Index + $match.Groups["body"].Length)

# UTF-8 no BOM
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Resolve-Path $path), $updated, $utf8NoBom)

Write-Host "OK: replaced review_summary prompt body with authoritative single-copy template" -ForegroundColor Green

python -m compileall .\rag\service.py
if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

pytest -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }

Write-Host "OK: tests pass" -ForegroundColor Green

Write-Host "== PROOF (counts) ==" -ForegroundColor Cyan
rg -n "if m == ""review_summary""|SECTIONS \(exact order\)|STAKEHOLDERS & OWNERSHIP|RISK REGISTER \(TOP 8\)|RETRIEVED CONTEXT" .\rag\service.py
