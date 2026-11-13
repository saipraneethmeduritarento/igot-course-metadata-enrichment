# Run any folder of course folders through the metadata pipeline.
#
# -Engine picks the code version under src\scripts\ (default v3.6, the engine
# these scripts ran before the move to version folders).
#
# Same shape as run_scorm.ps1 / run_non_scorm.ps1, with the input generalised:
# instead of a fixed content set with a prebuilt manifest, -CourseDir names any
# folder holding course id folders, in either layout find_course_dirs accepts:
#
#     <dir>/do_<id>/...            flat
#     <dir>/<batch>/do_<id>/...    as extracted from the batch zips
#
# The manifest the pipeline needs is built from that folder on first use with
# tools/inventory.py, which also collapses ids duplicated across overlapping
# batch zips. It is cached under logs\manifests; pass -Rescan to rebuild it
# after the folder's contents change.
#
# -Set names the run: it becomes CONTENT_SET (its own DB queue), the export
# folder, and the log file names. It defaults to the input folder's name, so
# two different folders never share a queue by accident.
#
#   .\src\scripts\run_courses.ps1 -CourseDir D:\courses\sep15                 # dry run
#   .\src\scripts\run_courses.ps1 -CourseDir D:\courses\sep15 -Execute        # real AI calls + DB writes
#   .\src\scripts\run_courses.ps1 -CourseDir D:\courses\sep15 -Execute -Set sep15 -Limit 20 -Concurrency 8
#   .\src\scripts\run_courses.ps1 -CourseDir D:\courses\sep15 -Execute -Api   # fetch content from the Learning API
#   .\src\scripts\run_courses.ps1 -CourseDir D:\courses\sep15 -Execute -Force # redo courses already done for this version
#   .\src\scripts\run_courses.ps1 -CourseDir D:\courses\sep15 -Execute -OutDir D:\exports\sep15   # exports elsewhere
#   .\src\scripts\run_courses.ps1 -CourseDir D:\courses\sep15 -Execute -Engine v3.7   # run the v3.7 engine

param(
    [Parameter(Mandatory = $true)]
    [string] $CourseDir,
    [string] $Set,
    [switch] $Execute,
    [switch] $Force,
    [switch] $Api,
    [switch] $Rescan,
    [int]    $Limit,
    [int]    $Concurrency = 8,
    [string] $OutDir,
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
$py = Join-Path $root '.venv\Scripts\python.exe'

# A relative -CourseDir is resolved against the caller's cwd; an absolute one is
# taken as-is (Join-Path would splice the two and produce nonsense).
$courseDir = if ([System.IO.Path]::IsPathRooted($CourseDir)) { $CourseDir }
             else { Join-Path (Get-Location).Path $CourseDir }
if (-not (Test-Path -LiteralPath $courseDir -PathType Container)) {
    throw "no such course folder: $courseDir"
}
$courseDir = (Resolve-Path -LiteralPath $courseDir).Path

# The run name defaults to the input folder's, lowercased and punctuation-free
# so it is safe in a path, an env var and a DB value alike.
if (-not $Set) {
    $Set = (Split-Path -Leaf $courseDir).ToLower() -replace '[^a-z0-9]+', '-' -replace '^-|-$', ''
}
if (-not $Set) { throw "could not derive a run name from '$courseDir'; pass -Set" }
# 'scorm' and 'non-scorm' are the two dedicated scripts' queues. Reusing either
# here would interleave this folder's checkpoints with theirs and make both
# runs' progress counts wrong.
if ($Set -in @('scorm', 'non-scorm')) {
    throw "'$Set' is reserved for run_$($Set -replace '-','_').ps1; pass a different -Set."
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$idFile = Join-Path $root "logs\$Set`_ids.txt"
$outCsv = Join-Path $root "logs\$Set-outcomes-$stamp.csv"
$manifest = Join-Path $root "logs\manifests\$Set.jsonl"
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
    Join-Path $root "output\runs\$Set"
}

# CONTENT_SET keeps this folder's checkpoints in their own queue, separate from
# every other run. Shell vars win over .env: load_dotenv() does not override.
$env:CONTENT_SET = $Set

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
    # nothing is read out of the folders themselves. find_course_dirs, not a
    # plain directory listing: under the nested layout the top-level folders are
    # batch names, not course ids. -Unique because the same course can sit in
    # two overlapping batch folders.
    $env:API_CACHE_PATH = Join-Path $root "api_course_cache\$Set"
    $lister = @'
import sys
from pathlib import Path
from course_io import find_course_dirs
for d in find_course_dirs(Path(sys.argv[1])):
    print(d.name)
'@
    # WriteAllLines, not Out-File: PS 5.1's utf8 encoder emits a BOM, which ends
    # up glued to the first id and makes that course 404 against the API.
    $ids = & $py '-c' $lister $courseDir |
        Sort-Object -Unique
    if ($LASTEXITCODE -ne 0) { throw "could not list course folders under $courseDir" }
    if (-not $ids) { throw "no course folders found under $courseDir" }
    [System.IO.File]::WriteAllLines($idFile, $ids, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "source: Learning API | $Set course ids: $($ids.Count) -> $idFile"
    $genArgs += @('--api', '--course-id-file', $idFile)
} else {
    # discover_courses() prefers COURSE_MANIFEST over scanning COURSES_BASE_PATH,
    # and the manifest's own `path` per course is what gets read -- so pointing
    # COURSES_BASE_PATH at this folder without a manifest of its own would
    # silently still run whatever set the .env manifest names.
    #
    # Unlike the two fixed sets, an arbitrary folder has no manifest yet, so it
    # is built here once and cached. It is a content scan, so it is not redone
    # on every run; -Rescan picks up folder changes.
    if ($Rescan -and (Test-Path $manifest)) { Remove-Item -Force $manifest }
    if (-not (Test-Path $manifest)) {
        New-Item -ItemType Directory -Force (Split-Path $manifest) | Out-Null
        Write-Host "building manifest for $courseDir ..."
        & $py (Join-Path $PSScriptRoot 'tools\inventory.py') --staging $courseDir --out $manifest
        if ($LASTEXITCODE -ne 0) { throw "inventory.py failed for $courseDir" }
    }
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
& $py @genArgs
$genExit = $LASTEXITCODE
$sw.Stop()

Write-Host ''
Write-Host ("TOTAL WALL TIME (generation): {0:hh\:mm\:ss}" -f $sw.Elapsed)
Write-Host "per-course outcomes: $outCsv"

if ($genExit -ne 0) { Write-Host "meta_gen exited $genExit -- skipping export"; exit $genExit }
if (-not $Execute)  { Write-Host 'dry run: nothing written, nothing to export'; exit 0 }

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
    & $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set $Set --ids-from $runJsonl --format csv  --out $exportDir
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

& $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set $Set --format json  --out $tmpDir   # one JSON per course + index
& $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set $Set --format jsonl --out $tmpDir   # single file, one course/line
& $py (Join-Path $PSScriptRoot 'tools\export.py') --content-set $Set --format csv   --out $tmpDir   # flat spreadsheet summary

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
