"""
Proofread completed jobs, fix errors, and mark as Ready to Invoice.

This script:
1. Fetches completed jobs and their timesheet notes
2. Proofreads all text for spelling/grammar errors
3. Updates the corrected text back to AroFlo
4. Marks jobs as Ready to Invoice

Usage:
    python proofread_and_mark_ready.py           # Dry run - show what would be done
    python proofread_and_mark_ready.py --apply   # Actually apply changes
"""

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
        if job_no:
            if job_no not in by_job:
                by_job[job_no] = []
            by_job[job_no].append({
                "timesheetid": ts.get("timesheetid"),
                "note": ts.get("note", ""),
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
                        "note": ts["note"],
                        "corrected_note": corrected_note,
                        "changed": ts["note"] != corrected_note,
                    })
                else:
                    corrected_timesheets.append({
                        "timesheetid": ts["timesheetid"],
                        "note": "",
                        "corrected_note": "",
                        "changed": False,
                    })
        else:
            corrected_timesheets = [{
                "timesheetid": ts["timesheetid"],
                "note": ts["note"],
                "corrected_note": ts["note"],
                "changed": False,
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

    # Step 5: Apply corrections
    print("\n5. Applying corrections...")
    corrections_made = 0

    for job in jobs_to_process:
        if not job["has_errors"]:
            continue

        task_name = job["task_name"]

        print(f"\n   --- {task_name} ---")

        # Update description if changed
        if job.get("description_changed") and job["corrected_description"]:
            try:
                print(f"   Description:")
                print(f"     BEFORE: {job['description'][:120]}")
                print(f"     AFTER:  {job['corrected_description'][:120]}")
                connector.update_task_description(job["task_id"], job["corrected_description"])
                print(f"   [OK] Updated description")
                corrections_made += 1
            except Exception as e:
                print(f"   [FAIL] Description update: {e}")

        # Update timesheet notes if changed
        for ts in job["timesheet_entries"]:
            if ts.get("changed") and ts["corrected_note"]:
                try:
                    print(f"   Timesheet note:")
                    print(f"     BEFORE: {ts['note'][:120]}")
                    print(f"     AFTER:  {ts['corrected_note'][:120]}")
                    connector.update_timesheet_note(ts["timesheetid"], ts["corrected_note"])
                    print(f"   [OK] Updated")
                    corrections_made += 1
                except Exception as e:
                    print(f"   [FAIL] Timesheet update: {e}")

    print(f"\n   Corrections applied: {corrections_made}")

    # Step 6: Mark all as Ready to Invoice
    print("\n6. Marking jobs as Ready to Invoice...")
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
    print(f"  Corrections made: {corrections_made}")
    print(f"  Marked ready: {marked}")
    print(f"  Failed: {failed}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
