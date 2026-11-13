# Run the full non-SCORM course set through the v3.5 pipeline.
#
# Same shape as run_scorm.ps1, pointed at the non-scorm set. The two are kept
# separate on purpose: CONTENT_SET puts each set in its own DB queue, and every
# output path below is namespaced so neither run overwrites the other's files.
#
# Content comes from the local export by default (input/courses/non-scorm, via
# the non-scorm.win.jsonl manifest). Pass -Api to fetch from the Learning API.
#
#   .\run_non_scorm.ps1                 # dry run: builds prompts, calls nothing
#   .\run_non_scorm.ps1 -Execute        # real AI calls + DB writes
#   .\run_non_scorm.ps1 -Execute -Limit 20 -Concurrency 8
#   .\run_non_scorm.ps1 -Execute -Api   # fetch content from the Learning API
#   .\run_non_scorm.ps1 -Execute -Force # redo courses already done for this version
#   .\run_non_scorm.ps1 -Execute -OutDir D:\exports\non-scorm   # exports elsewhere

param(
    [switch] $Execute,
    [switch] $Force,
    [switch] $Api,
    [int]    $Limit,
    [int]    $Concurrency = 8,
    [string] $OutDir,
    # Folder holding courses\ and manifests\. Defaults to $env:INPUT_ROOT, then
    # to the 'input' folder beside this repo.
    [string] $InputRoot
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$inputRoot = if ($InputRoot) { $InputRoot }
             elseif ($env:INPUT_ROOT) { $env:INPUT_ROOT }
             else { Join-Path (Split-Path $root -Parent) 'input' }
$courseDir = Join-Path $inputRoot 'courses\non-scorm'
$manifest = Join-Path $inputRoot 'manifests\non-scorm.win.jsonl'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$idFile = Join-Path $root 'logs\non-scorm_ids.txt'
$outCsv = Join-Path $root "logs\non-scorm-outcomes-$stamp.csv"
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
    Join-Path $root 'output\runs\non-scorm'
}

# CONTENT_SET keeps the non-scorm checkpoints in their own queue, separate from
# the scorm run. Shell vars win over .env: load_dotenv() does not override.
# Changing only the folder path and leaving this would file the results under
# the wrong set.
$env:CONTENT_SET = 'non-scorm'

New-Item -ItemType Directory -Force (Join-Path $root 'logs') | Out-Null

$genArgs = @('meta_gen.py', '--batch-size', $Concurrency, '--out', $outCsv)

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
    $env:API_CACHE_PATH = Join-Path $root 'api_course_cache\non-scorm'
    # WriteAllLines, not Out-File: PS 5.1's utf8 encoder emits a BOM, which ends
    # up glued to the first id and makes that course 404 against the API.
    $ids = Get-ChildItem -Path $courseDir -Directory |
        Select-Object -ExpandProperty Name |
        Sort-Object
    [System.IO.File]::WriteAllLines($idFile, $ids, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "source: Learning API | non-scorm course ids: $($ids.Count) -> $idFile"
    $genArgs += @('--api', '--course-id-file', $idFile)
} else {
    # discover_courses() prefers COURSE_MANIFEST over scanning COURSES_BASE_PATH,
    # and the manifest's own `path` per course is what gets read -- so pointing
    # COURSES_BASE_PATH at non-scorm without the manifest would silently run
    # whatever set the .env manifest names.
    if (-not (Test-Path $manifest)) { throw "missing manifest: $manifest" }
    $env:COURSE_MANIFEST = $manifest
    $env:COURSES_BASE_PATH = $courseDir
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
    & $py 'tools/export.py' --content-set non-scorm --ids-from $runJsonl --format csv  --out $exportDir
    & $py 'tools/review_report.py' --ids-from $runJsonl --out (Join-Path $exportDir 'review.html')
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

& $py 'tools/export.py' --content-set non-scorm --format json  --out $tmpDir   # one JSON per course + index
& $py 'tools/export.py' --content-set non-scorm --format jsonl --out $tmpDir   # single file, one course/line
& $py 'tools/export.py' --content-set non-scorm --format csv   --out $tmpDir   # flat spreadsheet summary

# Scope the review page to the courses that actually have records: review_report
# has no --content-set filter, so it needs an explicit id list, and feeding it
# the full manifest would print a "no record" warning per ungenerated course.
$doneIds = (Get-Content (Join-Path $tmpDir 'index.json') -Raw | ConvertFrom-Json).courses |
    ForEach-Object { $_.course_id }
$reviewIds = Join-Path $tmpDir 'review_ids.txt'
[System.IO.File]::WriteAllLines($reviewIds, $doneIds, (New-Object System.Text.UTF8Encoding($false)))
& $py 'tools/review_report.py' --ids-from $reviewIds --out (Join-Path $tmpDir 'review.html')
Remove-Item $reviewIds

# The swap deletes $exportDir wholesale, so refuse any target that holds
# something that is not a previous export. index.json is the marker; without it
# a mistyped path would be wiped.
if (Test-Path $exportDir) {
    $existing = @(Get-ChildItem -Force $exportDir)
    if ($existing.Count -and -not (Test-Path (Join-Path $exportDir 'index.json'))) {
        Remove-Item -Recurse -Force $tmpDir
        throw "refusing to overwrite '$exportDir': it is not empty and holds no index.json, so it is not a previous export."
    }
    Remove-Item -Recurse -Force $exportDir
}
New-Item -ItemType Directory -Force (Split-Path $exportDir) | Out-Null
Move-Item $tmpDir $exportDir

Write-Host ''
Write-Host "exports    : $exportDir  ($($doneIds.Count) courses)"
Write-Host "review page: $(Join-Path $exportDir 'review.html')"
Write-Host ("TOTAL WALL TIME (generation): {0:hh\:mm\:ss}" -f $sw.Elapsed)
