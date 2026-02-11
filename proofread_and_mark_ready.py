"""
Proofread completed jobs, fix errors, and mark as Ready to Invoice.

This script:
1. Fetches completed jobs and their timesheet notes
2. Proofreads all text for spelling/grammar errors
3. Auto-fixes task descriptions via the API
4. Prints manual correction list for timesheet notes (API doesn't support updating these)
5. Marks jobs as Ready to Invoice

Usage:
    python proofread_and_mark_ready.py           # Dry run - show what would be done
    python proofread_and_mark_ready.py --apply   # Actually apply changes
"""

import difflib
import sys
from aroflo_connector import create_connector
from proofreader import Proofreader


def get_timesheets_by_job(connector):
    """Get all timesheets grouped by job number."""
    response = connector.request("timesheets", {"page": 1})
    zr = response.get("zoneresponse", {})
    timesheets = zr.get("timesheets", [])

    if not isinstance(timesheets, list):
        timesheets = [timesheets] if timesheets else []

    # Group by job number
    by_job = {}
    for ts in timesheets:
        task_info = ts.get("task", {})
        job_no = task_info.get("jobnumber", "") if isinstance(task_info, dict) else ""
        task_id = task_info.get("taskid", "") if isinstance(task_info, dict) else ""
        if job_no:
            if job_no not in by_job:
                by_job[job_no] = []
            user_info = ts.get("user", {})
            user_name = ""
            if isinstance(user_info, dict):
                given = user_info.get("givennames", "")
                surname = user_info.get("surname", "")
                user_name = f"{given} {surname}".strip()
            by_job[job_no].append({
                "timesheetid": ts.get("timesheetid"),
                "task_id": task_id,
                "note": ts.get("note", ""),
                "user": user_name,
                "workdate": ts.get("workdate", ""),
                "starttime": ts.get("startdatetime", ""),
            })

    return by_job


def main():
    apply_changes = "--apply" in sys.argv

    print("Proofread, Fix, and Mark Ready to Invoice")
    print("=" * 60)

    if not apply_changes:
        print("DRY RUN MODE - No changes will be made")
        print("Run with --apply to make changes")
    print()

    connector = create_connector()
    proofreader = Proofreader(connector)

    # Step 1: Get Ready to Invoice substatus ID
    print("1. Getting substatus ID...")
    substatus_id = connector.get_substatus_id("Ready to Invoice")
    if not substatus_id:
        print("   ERROR: Could not find 'Ready to Invoice' substatus")
        return 1
    print(f"   Found: Ready to Invoice")

    # Step 2: Get completed tasks
    print("\n2. Fetching completed tasks...")
    response = connector.request("tasks", {"where": "and|status|=|Completed"})
    zr = response.get("zoneresponse", response)
    tasks = zr.get("tasks", [])

    if not tasks:
        print("   No completed tasks found")
        return 0

    print(f"   Found {len(tasks)} completed tasks")

    # Step 3: Get timesheets grouped by job
    print("\n3. Fetching timesheets...")
    timesheets_by_job = get_timesheets_by_job(connector)
    print(f"   Found timesheets for {len(timesheets_by_job)} jobs")

    # Step 4: Proofread each job
    print("\n4. Proofreading jobs...")
    jobs_to_process = []

    for task in tasks:
        task_id = task.get("taskid")
        task_name = task.get("taskname", "Unnamed")
        job_no = task.get("jobnumber", "")
        description = task.get("description", "")

        # Get timesheet notes for this job
        timesheet_entries = timesheets_by_job.get(job_no, [])
        labour_notes = "\n\n".join([ts["note"] for ts in timesheet_entries if ts["note"]])

        # Combine text for proofreading
        all_text = []
        if description:
            all_text.append(description)
        if labour_notes:
            all_text.append(labour_notes)

        combined_text = "\n\n".join(all_text)

        if not combined_text.strip():
            print(f"   [ ] {task_name[:40]} - No text to check")
            jobs_to_process.append({
                "task_id": task_id,
                "task_name": task_name,
                "job_no": job_no,
                "has_errors": False,
                "description": description,
                "corrected_description": description,
                "timesheet_entries": timesheet_entries,
                "corrected_timesheets": timesheet_entries,
            })
            continue

        # Proofread the text
        try:
            corrected_text, errors = proofreader._check_text(combined_text)
            has_errors = len(errors) > 0
        except Exception as e:
            print(f"   [!] {task_name[:40]} - Error checking: {e}")
            has_errors = False
            corrected_text = combined_text

        if has_errors:
            print(f"   [X] {task_name[:40]} - {len(errors)} error(s)")
            for err in errors:
                msg = err.get("message", "")
                suggestions = err.get("suggestions", [])
                context = err.get("context", "")
                suggestion_str = f" -> {suggestions[0]}" if suggestions else ""
                print(f"       - {msg}{suggestion_str}")
        else:
            print(f"   [OK] {task_name[:40]}")

        # Split corrected text back into description and timesheet notes
        corrected_description = description
        corrected_timesheets = []

        if has_errors:
            # For simplicity, proofread each part separately to get individual corrections
            if description:
                try:
                    corrected_description, _ = proofreader._check_text(description)
                except:
                    corrected_description = description

            for ts in timesheet_entries:
                if ts["note"]:
                    try:
                        corrected_note, _ = proofreader._check_text(ts["note"])
                    except:
                        corrected_note = ts["note"]
                    corrected_timesheets.append({
                        "timesheetid": ts["timesheetid"],
                        "task_id": ts.get("task_id", ""),
                        "note": ts["note"],
                        "corrected_note": corrected_note,
                        "changed": ts["note"] != corrected_note,
                        "user": ts.get("user", ""),
                        "workdate": ts.get("workdate", ""),
                        "starttime": ts.get("starttime", ""),
                    })
                else:
                    corrected_timesheets.append({
                        "timesheetid": ts["timesheetid"],
                        "task_id": ts.get("task_id", ""),
                        "note": "",
                        "corrected_note": "",
                        "changed": False,
                        "user": ts.get("user", ""),
                        "workdate": ts.get("workdate", ""),
                        "starttime": ts.get("starttime", ""),
                    })
        else:
            corrected_timesheets = [{
                "timesheetid": ts["timesheetid"],
                "task_id": ts.get("task_id", ""),
                "note": ts["note"],
                "corrected_note": ts["note"],
                "changed": False,
                "user": ts.get("user", ""),
                "workdate": ts.get("workdate", ""),
                "starttime": ts.get("starttime", ""),
            } for ts in timesheet_entries]

        jobs_to_process.append({
            "task_id": task_id,
            "task_name": task_name,
            "job_no": job_no,
            "has_errors": has_errors,
            "error_count": len(errors) if has_errors else 0,
            "description": description,
            "corrected_description": corrected_description,
            "description_changed": description != corrected_description,
            "timesheet_entries": corrected_timesheets,
        })

    # Summary
    jobs_with_errors = [j for j in jobs_to_process if j["has_errors"]]
    print(f"\n   Total: {len(jobs_to_process)} jobs")
    print(f"   With errors: {len(jobs_with_errors)} jobs")

    if not apply_changes:
        print("\n" + "=" * 60)
        print("DRY RUN COMPLETE")
        print("Run with --apply to fix errors and mark jobs ready")
        return 0

    # Step 5: Apply description corrections (API supports this)
    print("\n5. Applying task description corrections...")
    descriptions_fixed = 0

    for job in jobs_to_process:
        if not job.get("description_changed") or not job["corrected_description"]:
            continue

        task_name = job["task_name"]
        try:
            print(f"   {task_name}:")
            print(f"     BEFORE: {job['description'][:120]}")
            print(f"     AFTER:  {job['corrected_description'][:120]}")
            connector.update_task_description(job["task_id"], job["corrected_description"])
            print(f"     [OK] Updated")
            descriptions_fixed += 1
        except Exception as e:
            print(f"     [FAIL] {e}")

    if descriptions_fixed == 0:
        print("   No description corrections needed.")
    else:
        print(f"   Descriptions fixed: {descriptions_fixed}")

    # Step 6: Show manual corrections needed for timesheet notes
    # (AroFlo API does not support updating timesheet notes)
    manual_corrections = []
    for job in jobs_to_process:
        if not job["has_errors"]:
            continue
        for ts in job["timesheet_entries"]:
            if ts.get("changed") and ts["corrected_note"]:
                manual_corrections.append({
                    "job": job["task_name"],
                    "user": ts.get("user", "Unknown"),
                    "workdate": ts.get("workdate", ""),
                    "starttime": ts.get("starttime", ""),
                    "before": ts["note"],
                    "after": ts["corrected_note"],
                })

    if manual_corrections:
        print(f"\n6. MANUAL CORRECTIONS NEEDED ({len(manual_corrections)} timesheet notes)")
        print("   (AroFlo API does not support updating timesheet notes)")
        print("-" * 60)

        for i, fix in enumerate(manual_corrections, 1):
            # Format the date/time for easy identification
            date_str = fix["workdate"]
            time_str = ""
            if fix["starttime"]:
                # startdatetime is like "2026/01/14 10:00:00"
                parts = fix["starttime"].split(" ")
                if len(parts) == 2:
                    time_str = parts[1][:5]  # "10:00"

            print(f"\n   [{i}] {fix['job']}")
            print(f"       Employee: {fix['user']}")
            print(f"       Date: {date_str}  Start: {time_str}")

            # Show word-level diffs with surrounding context
            old_words = fix["before"].split()
            new_words = fix["after"].split()
            sm = difflib.SequenceMatcher(None, old_words, new_words)
            changes = []
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op == "equal":
                    continue
                old_phrase = " ".join(old_words[i1:i2])
                new_phrase = " ".join(new_words[j1:j2])
                # Add a word of context before/after
                ctx_before = old_words[max(0, i1-1):i1]
                ctx_after = old_words[i2:min(len(old_words), i2+1)]
                ctx = ""
                if ctx_before:
                    ctx += f"...{ctx_before[0]} "
                ctx += f'"{old_phrase}" -> "{new_phrase}"'
                if ctx_after:
                    ctx += f" {ctx_after[0]}..."
                changes.append(ctx)

            if changes:
                for change in changes:
                    print(f"       Fix: {change}")
            else:
                print(f"       Fix: spacing/whitespace change")

        print("\n" + "-" * 60)
    else:
        print("\n6. No manual timesheet corrections needed.")

    # Step 7: Mark all as Ready to Invoice
    print(f"\n7. Marking jobs as Ready to Invoice...")
    marked = 0
    failed = 0

    for job in jobs_to_process:
        # Check current substatus
        task_id = job["task_id"]
        task_name = job["task_name"]

        try:
            connector.update_task_substatus(task_id, substatus_id)
            print(f"   [OK] {task_name[:50]}")
            marked += 1
        except Exception as e:
            print(f"   [FAIL] {task_name[:50]} - {e}")
            failed += 1

    print(f"\n" + "=" * 60)
    print(f"COMPLETE")
    print(f"  Jobs proofread: {len(jobs_to_process)}")
    print(f"  Descriptions auto-fixed: {descriptions_fixed}")
    print(f"  Timesheet notes to fix manually: {len(manual_corrections)}")
    print(f"  Marked ready: {marked}")
    print(f"  Failed: {failed}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
