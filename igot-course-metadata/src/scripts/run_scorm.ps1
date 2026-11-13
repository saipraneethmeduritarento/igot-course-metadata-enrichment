# Run the full SCORM course set through the metadata pipeline.
#
# -Engine picks the code version under src\scripts\ (default v3.6, the engine
# these scripts ran before the move to version folders).
#
# Content comes from the local export by default (input/courses/scorm, via the
# scorm.win.jsonl manifest). Pass -Api to fetch from the Learning API instead.
# Local is the better default here: it carries the real scorm=true flag, has
# PDFs for 235 courses the API returns nothing for, and has no fetch timeouts.
#
#   .\src\scripts\run_scorm.ps1                 # dry run: builds prompts, calls nothing
#   .\src\scripts\run_scorm.ps1 -Execute        # real AI calls + DB writes
#   .\src\scripts\run_scorm.ps1 -Execute -Limit 20 -Concurrency 8
#   .\src\scripts\run_scorm.ps1 -Execute -Api   # fetch content from the Learning API
#   .\src\scripts\run_scorm.ps1 -Execute -Force # redo courses already done for this version
#   .\src\scripts\run_scorm.ps1 -Execute -OutDir D:\exports\scorm   # exports elsewhere
#   .\src\scripts\run_scorm.ps1 -Execute -Engine v3.7   # run the v3.7 engine

param(
    [switch] $Execute,
    [switch] $Force,
    [switch] $Api,
    [int]    $Limit,
    [int]    $Concurrency = 8,
    [string] $OutDir,
    # Folder holding courses\ and manifests\. Defaults to $env:INPUT_ROOT, then
    # to the 'input' folder beside this repo.
    [string] $InputRoot,
    # Engine version folder under src\scripts\ (v3.5, v3.6, v3.7, ...).
    [string] $Engine = 'v3.6'
)

$ErrorActionPreference = 'Stop'
# This script lives in src\scripts\; the project root (.venv, .env, logs\,
# output\) is two levels up.
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$metaGen = Join-Path $PSScriptRoot "$Engine\meta_gen.py"
if (-not (Test-Path $metaGen)) { throw "no engine '$Engine' (expected $metaGen)" }
if ($Engine -eq 'v3.7') {
    # v3.7 meta_gen tags its runs PROMPT_VERSION_V37 (default v3.7) whatever .env
    # says; export.py and review_report.py must read the same tag.
    $env:PROMPT_VERSION = if ($env:PROMPT_VERSION_V37) { $env:PROMPT_VERSION_V37 } else { 'v3.7' }
}
$inputRoot = if ($InputRoot) { $InputRoot }
             elseif ($env:INPUT_ROOT) { $env:INPUT_ROOT }
             else { Join-Path (Split-Path $root -Parent) 'input' }
$scormDir = Join-Path $inputRoot 'courses\scorm'
$manifest = Join-Path $inputRoot 'manifests\scorm.win.jsonl'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$idFile = Join-Path $root 'logs\scorm_ids.txt'
$outCsv = Join-Path $root "logs\scorm-outcomes-$stamp.csv"
# One stable home for this set, rewritten in place on every run (with or
# without -Force) so it always holds the complete current export and never
# accumulates timestamped copies. Nothing else writes here.
# -OutDir moves it elsewhere; the folder is still rewritten in place.
$exportDir = if ($OutDir) {
    # A relative -OutDir is resolved against the caller's cwd; an absolute one
    # is taken as-is (Join-Path would splice the two and produce nonsense).
    if ([System.IO.Path]::IsPathRooted($OutDir)) { $OutDir }
    else { Join-Path (Get-Location).Path $OutDir }
} else {
    Join-Path $root 'output\runs\scorm'
}

# CONTENT_SET keeps the scorm checkpoints in their own queue, separate from the
# non-scorm run. Shell vars win over .env: load_dotenv() does not override.
$env:CONTENT_SET = 'scorm'

New-Item -ItemType Directory -Force (Join-Path $root 'logs') | Out-Null

$genArgs = @($metaGen, '--batch-size', $Concurrency, '--out', $outCsv)

# -OutDir collects generated courses. meta_gen writes each one into the folder
# as it commits, so the folder fills live and survives an interrupted run.
# Nothing here is cleared: successive runs accumulate, and the folder ends up
# holding every course generated into it rather than just the last run's.
if ($OutDir -and $Execute) {
    $genArgs += @('--run-dir', $exportDir)
}

if ($Api) {
    # --api takes an explicit id list; the folder names are the id source, but
    # nothing is read out of the folders themselves.
    $env:API_CACHE_PATH = Join-Path $root 'api_course_cache\scorm'
    # WriteAllLines, not Out-File: PS 5.1's utf8 encoder emits a BOM, which ends
    # up glued to the first id and makes that course 404 against the API.
    $ids = Get-ChildItem -Path $scormDir -Directory |
        Select-Object -ExpandProperty Name |
        Sort-Object
    [System.IO.File]::WriteAllLines($idFile, $ids, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "source: Learning API | scorm course ids: $($ids.Count) -> $idFile"
    $genArgs += @('--api', '--course-id-file', $idFile)
} else {
    # discover_courses() prefers COURSE_MANIFEST over scanning COURSES_BASE_PATH,
    # and the manifest's own `path` per course is what gets read -- so pointing
    # COURSES_BASE_PATH at scorm without the manifest would silently still run
    # the non-scorm set.
    if (-not (Test-Path $manifest)) { throw "missing manifest: $manifest" }
    $env:COURSE_MANIFEST = $manifest
    $env:COURSES_BASE_PATH = $scormDir
    $count = (Get-Content $manifest | Measure-Object -Line).Lines
    Write-Host "source: local files | manifest: $manifest ($count courses)"
    # review_report.py --ids-from wants the id list too; take it from the manifest
    # so it matches exactly what ran.
    $ids = Get-Content $manifest | ForEach-Object { ($_ | ConvertFrom-Json).course_id }
    [System.IO.File]::WriteAllLines($idFile, $ids, (New-Object System.Text.UTF8Encoding($false)))
}

if ($Execute) { $genArgs += '--execute' }
if ($Force)   { $genArgs += '--force' }
if ($Limit)   { $genArgs += @('--limit', $Limit) }

$sw = [System.Diagnostics.Stopwatch]::StartNew()
& (Join-Path $root '.venv\Scripts\python.exe') @genArgs
$genExit = $LASTEXITCODE
$sw.Stop()

Write-Host ''
Write-Host ("TOTAL WALL TIME (generation): {0:hh\:mm\:ss}" -f $sw.Elapsed)
Write-Host "per-course outcomes: $outCsv"

if ($genExit -ne 0) { Write-Host "meta_gen exited $genExit -- skipping export"; exit $genExit }
if (-not $Execute)  { Write-Host 'dry run: nothing written, nothing to export'; exit 0 }

$py = Join-Path $root '.venv\Scripts\python.exe'

if ($OutDir) {
    # -OutDir: meta_gen has already written courses/<id>.json and records.jsonl
    # live, one per course as it committed, adding to whatever earlier runs left
    # here. The summary files are then rebuilt over everything the folder holds,
    # so csv and review.html always describe its full current contents.
    # No .tmp swap: the per-course files are the real output and must not be
    # taken away and put back.
    $runJsonl = Join-Path $exportDir 'records.jsonl'
    if (-not (Test-Path $runJsonl)) {
        Write-Host "no courses in $exportDir yet; nothing to summarise"
        Write-Host ("TOTAL WALL TIME (generation): {0:hh\:mm\:ss}" -f $sw.Elapsed)
        exit 0
    }
    & $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set scorm --ids-from $runJsonl --format csv  --out $exportDir
    & $py (Join-Path $PSScriptRoot 'tools\review_report.py') --ids-from $runJsonl --out (Join-Path $exportDir 'review.html')
    $n = @(Get-ChildItem (Join-Path $exportDir 'courses') -Filter *.json).Count

    Write-Host ''
    Write-Host "exports    : $exportDir  ($n courses in folder)"
    Write-Host "review page: $(Join-Path $exportDir 'review.html')"
    Write-Host ("TOTAL WALL TIME (generation): {0:hh\:mm\:ss}" -f $sw.Elapsed)
    exit 0
}

# Default: the stable per-set folder, holding the complete content set. The rows
# live in Postgres either way; this is the portable copy.
#
# Built in a sibling .tmp folder and swapped in only once every step has
# succeeded, so a failed export can never leave the stable folder half-written
# or empty. export.py emits the whole content set, not just this run's courses.
$tmpDir = "$exportDir.tmp"
if (Test-Path $tmpDir) { Remove-Item -Recurse -Force $tmpDir }
New-Item -ItemType Directory -Force $tmpDir | Out-Null

& $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set scorm --format json  --out $tmpDir   # one JSON per course + index
& $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set scorm --format jsonl --out $tmpDir   # single file, one course/line
& $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set scorm --format csv   --out $tmpDir   # flat spreadsheet summary

# Scope the review page to the courses that actually have records: review_report
# has no --content-set filter, so it needs an explicit id list, and feeding it
# the full manifest would print a "no record" warning per ungenerated course.
$doneIds = (Get-Content (Join-Path $tmpDir 'index.json') -Raw | ConvertFrom-Json).courses |
    ForEach-Object { $_.course_id }
$reviewIds = Join-Path $tmpDir 'review_ids.txt'
[System.IO.File]::WriteAllLines($reviewIds, $doneIds, (New-Object System.Text.UTF8Encoding($false)))
& $py (Join-Path $PSScriptRoot 'tools\review_report.py') --ids-from $reviewIds --out (Join-Path $tmpDir 'review.html')
Remove-Item $reviewIds

# The swap deletes $exportDir wholesale, so refuse any target that holds
# something that is not a previous export. index.json is the marker; without it
# a mistyped -OutDir (a Desktop, a Downloads) would be wiped.
if (Test-Path $exportDir) {
    $existing = @(Get-ChildItem -Force $exportDir)
    if ($existing.Count -and -not (Test-Path (Join-Path $exportDir 'index.json'))) {
        Remove-Item -Recurse -Force $tmpDir
        throw "refusing to overwrite '$exportDir': it is not empty and holds no index.json, so it is not a previous export. Pass a new or empty -OutDir."
    }
    Remove-Item -Recurse -Force $exportDir
}
New-Item -ItemType Directory -Force (Split-Path $exportDir) | Out-Null
Move-Item $tmpDir $exportDir

Write-Host ''
Write-Host "exports    : $exportDir  ($($doneIds.Count) courses)"
Write-Host "review page: $(Join-Path $exportDir 'review.html')"
Write-Host ("TOTAL WALL TIME (generation): {0:hh\:mm\:ss}" -f $sw.Elapsed)
