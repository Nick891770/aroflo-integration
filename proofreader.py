"""
Job Card Proofreader

Fetches completed but uninvoiced jobs from AroFlo and checks
the task notes/descriptions for spelling and grammar errors.
"""

import requests
from dataclasses import dataclass
from typing import Optional

# LanguageTool API (no Java required - uses online API)
LANGUAGETOOL_API_AVAILABLE = True

# Try spellchecker as fallback (no Java required)
try:
    from spellchecker import SpellChecker
    SPELLCHECKER_AVAILABLE = True
except ImportError:
    SPELLCHECKER_AVAILABLE = False

# Fallback to language_tool_python (requires Java)
try:
    import language_tool_python
    LANGUAGE_TOOL_AVAILABLE = True
except ImportError:
    LANGUAGE_TOOL_AVAILABLE = False

from aroflo_connector import AroFloConnector, create_connector
from data_extractor import DataExtractor


@dataclass
class ProofreadResult:
    """Result of proofreading a job card."""

    job_id: str
    job_name: str
    original_text: str
    corrected_text: str
    errors: list
    has_errors: bool


class Proofreader:
    """Proofreads job card descriptions and notes."""

    def __init__(self, connector: Optional[AroFloConnector] = None):
        """
        Initialize the proofreader.

        Args:
            connector: AroFloConnector instance (creates one if not provided)
        """
        self.connector = connector or create_connector()
        self.extractor = DataExtractor(self.connector)
        self._tool = None
        self._spellchecker = None
        self._last_api_call = 0  # Timestamp for rate limiting

    def _get_spellchecker(self):
        """Get or initialize the spellchecker."""
        if not SPELLCHECKER_AVAILABLE:
            raise RuntimeError(
                "pyspellchecker is not installed. "
                "Run: pip install pyspellchecker"
            )

        if self._spellchecker is None:
            print("Initializing spell checker...")
            self._spellchecker = SpellChecker()
            # Add common electrical/trade terms to avoid false positives
            self._spellchecker.word_frequency.load_words([
                # Electrical terms
                "switchboard", "switchboards", "powerpoint", "powerpoints", "rcbo", "rcd", "mcb",
                "gpo", "gpos", "led", "leds", "downlight", "downlights", "db", "dbs",
                "batten", "battens", "fluoro", "fluoros", "fluorescent", "conduit", "conduits",
                "trunking", "cabling", "rewire", "rewiring", "submain", "submains",
                "isolator", "isolators", "contactor", "contactors", "breaker", "breakers",
                "switchgear", "busbar", "busbars", "earthing", "bonding", "spitfire",
                "weatherproof", "recessed", "ducting", "circuiting", "rewired",
                "estop", "e-stop", "thermalscan", "reterminate",
                "highbay", "high-bay", "fluro", "wifi", "wi-fi",
                # Tool/Equipment brands
                "makita", "dewalt", "metabo", "bosch", "hilti", "milwaukee", "ryobi",
                "festool", "hikoki", "hitachi", "telstra",
                # Australian location names (common in job descriptions)
                "wacol", "archerfield", "richlands", "rochedale", "stapylton",
                "karalee", "donga", "dongas", "bremer", "redbank", "buranda",
                # Trade terms
                "grinder", "grinders", "sandblast", "sandblasting",
                "callout", "callouts", "aroflo", "spartan",
                "lunchroom", "fitoff", "fit-off", "reece",
                # Common contractions (partial words from splitting)
                "couldn", "wouldn", "shouldn", "didn", "doesn", "isn", "aren", "weren",
                "hasn", "haven", "hadn", "won", "don", "can", "ll", "ve", "re",
            ])

        return self._spellchecker

    def _get_language_tool(self):
        """Get or initialize the language tool (requires Java)."""
        if not LANGUAGE_TOOL_AVAILABLE:
            raise RuntimeError(
                "language_tool_python is not installed. "
                "Run: pip install language-tool-python"
            )

        if self._tool is None:
            print("Initializing language tool (this may take a moment)...")
            self._tool = language_tool_python.LanguageTool("en-AU")

        return self._tool

    def _extract_text_from_job(self, job: dict) -> str:
        """
        Extract all text content from a job for proofreading.

        Args:
            job: Job dictionary from AroFlo API

        Returns:
            Combined text from job description, notes, etc.
        """
        text_parts = []

        # Common field names for text content
        text_fields = [
            "description",
            "notes",
            "tasknotes",
            "task_notes",
            "workperformed",
            "work_performed",
            "comments",
            "details",
            "summary",
            "labour_notes",  # Timesheet/labour notes
        ]

        for field in text_fields:
            value = job.get(field)
            if value and isinstance(value, str) and value.strip():
                text_parts.append(value.strip())

        # Also check nested structures
        if "notes" in job and isinstance(job["notes"], dict):
            for note in job["notes"].get("note", []):
                if isinstance(note, dict) and note.get("text"):
                    text_parts.append(note["text"])

        return "\n\n".join(text_parts)

    def _check_text(self, text: str) -> tuple[str, list]:
        """
        Check text for spelling and grammar errors.

        Args:
            text: Text to check

        Returns:
            Tuple of (corrected_text, list of errors)
        """
        if not text.strip():
            return text, []

        # Use LanguageTool API (no Java required, full grammar checking)
        if LANGUAGETOOL_API_AVAILABLE:
            try:
                return self._check_text_languagetool_api(text)
            except Exception as e:
                print(f"  (API unavailable, falling back to spell check: {e})")

        # Fall back to pyspellchecker (spelling only)
        if SPELLCHECKER_AVAILABLE:
            return self._check_text_spellchecker(text)

        # Fall back to language_tool if available (requires Java)
        if LANGUAGE_TOOL_AVAILABLE:
            return self._check_text_language_tool(text)

        raise RuntimeError(
            "No spell checker available. "
            "Run: pip install pyspellchecker"
        )

    # Trade-specific terms to ignore (case-insensitive)
    TRADE_TERMS = {
        # Electrical terms
        "gpo", "gpos", "rcbo", "rcd", "mcb", "db", "dbs", "led", "leds",
        "estop", "e-stop", "thermalscan", "submain", "submains",
        "busbar", "busbars", "switchgear", "powerpoint", "powerpoints",
        "reterminate", "reterminated", "highbay", "breezeway",
        "wifi", "3ph", "1ph", "fitoff", "fit-off", "lunchroom",
        # Tool/Equipment brands
        "makita", "dewalt", "metabo", "bosch", "hilti", "milwaukee",
        "ryobi", "festool", "hikoki", "hitachi", "telstra",
        "haas", "hass",  # Haas CNC machines
        # Australian location names (whitelisted for spell-check)
        "wacol", "archerfield", "richlands", "rochedale", "stapylton",
        "karalee", "bremer", "redbank", "buranda",
        # Trade terms
        "fluoro", "fluoros", "fluro", "callout", "callouts",
        "donga", "dongas", "spartan", "reece", "angus",
        # Australian English
        "power point", "powerpoint",
    }

    # Custom corrections for common misspellings that the API gets wrong
    # Maps lowercase misspelling to correct spelling
    CUSTOM_CORRECTIONS = {
        "lense": "lens",
        "lenses": "lenses",  # Already correct but ensure not changed
        "archefield": "Archerfield",
        "hass": "Haas",  # CNC machine brand
        "imput": "input",
        "prosessor": "processor",
        "conection": "connection",
        "dissasemble": "disassemble",
        "recieving": "receiving",
        "outler": "outlet",
        "wasnt": "wasn't",
        "didnt": "didn't",
        "did'nt": "didn't",
        "couldnt": "couldn't",
        "wouldnt": "wouldn't",
        "shouldnt": "shouldn't",
        "isnt": "isn't",
        "arent": "aren't",
        "werent": "weren't",
        "hasnt": "hasn't",
        "havent": "haven't",
        "hadnt": "hadn't",
        "dont": "don't",
        "wont": "won't",
        "cant": "can't",
        "its": "it's",  # Note: context-dependent, but usually means "it is" in these notes
        "andi": "and I",
        "thier": "their",
        "teh": "the",
        "taht": "that",
        "wiht": "with",
        "adn": "and",
        "hte": "the",
        "fo": "of",
        "nad": "and",
        "tiem": "time",
        "jsut": "just",
        "nto": "not",
        "ahve": "have",
        "waht": "what",
        "shoudl": "should",
        "woudl": "would",
        "coudl": "could",
    }

    # Suggestions to reject (when API suggests these, skip the correction)
    REJECT_SUGGESTIONS = {
        "powerpoint": ["PowerPoint"],  # Don't change to Microsoft PowerPoint
        "power point": ["PowerPoint"],
        "haas": ["has", "mass", "pass"],
        "hass": ["has", "mass", "pass"],
        "lense": ["sense", "dense", "lease"],  # We have our own correction
        "archefield": ["Wakefield", "archfiend"],  # We have our own correction
    }

    # Words that should NEVER be changed (common words the API incorrectly flags)
    PROTECTED_WORDS = {
        "into", "onto", "for", "of", "the", "and", "or", "to", "in", "on",
        "at", "by", "with", "from", "as", "is", "it", "be", "was", "were",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "must", "shall", "can",
        "go", "went", "gone", "follow", "followed", "following",
        "circuits", "circuit", "found", "find",  # Trade terms often flagged
        "before", "after", "tightened", "tight",
    }

    def _check_text_languagetool_api(self, text: str) -> tuple[str, list]:
        """Check text using LanguageTool public API (no Java required)."""
        import time
        url = "https://api.languagetool.org/v2/check"

        # Rate limit: wait at least 1.5s between API calls (free tier limit)
        elapsed = time.time() - self._last_api_call
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)
        self._last_api_call = time.time()

        # First, apply custom corrections to common misspellings
        pre_corrected = text
        import re
        for misspelling, correction in self.CUSTOM_CORRECTIONS.items():
            # Case-insensitive replacement with word boundaries to avoid partial matches
            # e.g., "fo" -> "of" should NOT match inside "for"
            pattern = re.compile(r'\b' + re.escape(misspelling) + r'\b', re.IGNORECASE)
            matches = list(pattern.finditer(pre_corrected))
            for m in reversed(matches):
                original = m.group()
                # Preserve case: if original starts with upper, capitalize correction
                if original[0].isupper() and correction[0].islower():
                    replacement = correction.capitalize()
                else:
                    replacement = correction
                pre_corrected = pre_corrected[:m.start()] + replacement + pre_corrected[m.end():]

        # Use Australian English
        data = {
            "text": pre_corrected,
            "language": "en-AU",
        }

        # Retry with exponential backoff (public API rate limits)
        max_retries = 3
        for attempt in range(max_retries):
            response = requests.post(url, data=data, timeout=30)
            if response.status_code == 200:
                break
            if response.status_code in (429, 500, 502, 503) and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2s, 4s
                print(f"  (API returned {response.status_code}, retrying in {wait}s...)")
                time.sleep(wait)
                continue
            response.raise_for_status()

        result = response.json()

        errors = []
        corrected = pre_corrected

        # Process matches (errors) in reverse order to preserve string positions
        matches = result.get("matches", [])
        for match in reversed(matches):
            offset = match.get("offset", 0)
            length = match.get("length", 0)
            replacements = match.get("replacements", [])
            message = match.get("message", "")
            rule_id = match.get("rule", {}).get("id", "")

            # Skip certain rule types that are too picky
            if rule_id in ["WHITESPACE_RULE", "UPPERCASE_SENTENCE_START"]:
                continue

            # Get the flagged word
            flagged_word = pre_corrected[offset:offset+length].lower().strip()

            # Skip if it's a known trade term
            if flagged_word in self.TRADE_TERMS:
                continue

            # Skip if it's a protected common word (API often wrongly flags these)
            if flagged_word in self.PROTECTED_WORDS:
                continue

            # Get suggestions
            suggestions = [r.get("value", "") for r in replacements[:3]]

            # Check if we should reject these suggestions (bad API corrections)
            if flagged_word in self.REJECT_SUGGESTIONS:
                rejected = self.REJECT_SUGGESTIONS[flagged_word]
                suggestions = [s for s in suggestions if s not in rejected]
                if not suggestions:
                    continue  # Skip this error entirely if all suggestions are bad

            # Reject suggestions that wrongly add apostrophes to plural words
            # e.g., "circuits" should not become "circuit's"
            if not flagged_word.endswith("'s"):
                suggestions = [s for s in suggestions if "'s" not in s]
                if not suggestions:
                    continue

            # Skip if the flagged word is a common English word being incorrectly flagged
            common_words_to_skip = {
                "for", "of", "or", "to", "the", "and", "a", "an", "in", "on", "at",
                "by", "with", "from", "as", "is", "it", "be", "was", "were", "are",
                "into", "onto", "follow", "followed", "following", "go", "went",
            }
            if flagged_word in common_words_to_skip:
                continue

            # Reject suggestions that are nonsense or inappropriate word swaps
            bad_replacements = {
                # Don't swap common prepositions/conjunctions for each other
                "of", "or", "for", "to", "not", "knot", "on", "an", "in", "at",
                # Nonsense suggestions
                "goo", "allow", "fellow", "flow",
                "ofr", "ofllow", "fro", "fo", "ot", "ont", "nto",
                "go not", "goo not",
            }
            suggestions = [s for s in suggestions if s.lower() not in bad_replacements]
            if not suggestions:
                continue

            # Reject any suggestion that looks like a typo (non-word)
            # Skip suggestions with unusual patterns that aren't real words
            import re as regex_module
            suggestions = [s for s in suggestions if not regex_module.match(r'^[bcdfghjklmnpqrstvwxz]{3,}', s.lower())]
            if not suggestions:
                continue

            # Check if we have a custom correction for this word
            if flagged_word in self.CUSTOM_CORRECTIONS:
                custom = self.CUSTOM_CORRECTIONS[flagged_word]
                suggestions = [custom] + [s for s in suggestions if s.lower() != custom.lower()]

            error_info = {
                "message": message,
                "context": pre_corrected[max(0, offset-10):offset+length+10],
                "suggestions": suggestions,
                "rule_id": rule_id,
                "offset": offset,
                "length": length,
            }
            errors.append(error_info)

            # Apply first suggestion to corrected text
            if suggestions:
                corrected = corrected[:offset] + suggestions[0] + corrected[offset+length:]

        # Reverse errors list so they're in reading order
        errors.reverse()

        return corrected, errors

    def _check_text_spellchecker(self, text: str) -> tuple[str, list]:
        """Check text using pyspellchecker."""
        import re

        spell = self._get_spellchecker()

        # Extract words (keep track of positions)
        words = re.findall(r'\b[a-zA-Z]+\b', text)

        # Find misspelled words
        misspelled = spell.unknown(words)

        errors = []
        corrected = text

        for word in misspelled:
            # Skip very short words and words that look like abbreviations
            if len(word) <= 2 or word.isupper():
                continue

            correction = spell.correction(word)
            candidates = spell.candidates(word)

            # Skip if no correction available or correction is same as original
            if not correction or correction.lower() == word.lower():
                continue

            error_info = {
                "message": f"Possible spelling error: '{word}'",
                "context": word,
                "suggestions": list(candidates)[:3] if candidates else [],
                "rule_id": "SPELLING",
                "word": word,
            }
            errors.append(error_info)

            # Apply correction (case-preserving)
            if word[0].isupper() and correction:
                correction = correction.capitalize()
            corrected = re.sub(r'\b' + re.escape(word) + r'\b', correction or word, corrected, count=1)

        return corrected, errors

    def _check_text_language_tool(self, text: str) -> tuple[str, list]:
        """Check text using language_tool_python (requires Java)."""
        tool = self._get_language_tool()

        # Get matches (errors)
        matches = tool.check(text)

        # Get corrected text
        corrected = language_tool_python.utils.correct(text, matches)

        # Format errors for display
        errors = []
        for match in matches:
            error_info = {
                "message": match.message,
                "context": match.context,
                "suggestions": match.replacements[:3] if match.replacements else [],
                "rule_id": match.ruleId,
                "offset": match.offset,
                "length": match.errorLength,
            }
            errors.append(error_info)

        return corrected, errors

    def proofread_job(self, job: dict) -> ProofreadResult:
        """
        Proofread a single job card.

        Args:
            job: Job dictionary from AroFlo API

        Returns:
            ProofreadResult with original and corrected text
        """
        job_id = str(job.get("id", job.get("jobid", job.get("taskid", "unknown"))))
        job_name = job.get("name", job.get("jobname", job.get("taskname", "Unnamed Job")))

        text = self._extract_text_from_job(job)

        if not text:
            return ProofreadResult(
                job_id=job_id,
                job_name=job_name,
                original_text="",
                corrected_text="",
                errors=[],
                has_errors=False,
            )

        corrected, errors = self._check_text(text)

        return ProofreadResult(
            job_id=job_id,
            job_name=job_name,
            original_text=text,
            corrected_text=corrected,
            errors=errors,
            has_errors=len(errors) > 0,
        )

    def proofread_uninvoiced_jobs(self) -> list[ProofreadResult]:
        """
        Fetch and proofread all completed but uninvoiced jobs.

        Returns:
            List of ProofreadResult objects
        """
        # Fetch uninvoiced jobs
        jobs = self.extractor.get_completed_uninvoiced_jobs()

        if not jobs:
            print("No completed uninvoiced jobs found.")
            return []

        print(f"\nProofreading {len(jobs)} job(s)...")

        results = []
        for i, job in enumerate(jobs, 1):
            print(f"  [{i}/{len(jobs)}] Checking job...", end=" ")
            result = self.proofread_job(job)
            print(f"{result.job_name} - {'Errors found' if result.has_errors else 'OK'}")
            results.append(result)

        return results

    def print_results(self, results: list[ProofreadResult], show_all: bool = False):
        """
        Print proofreading results to console.

        Args:
            results: List of ProofreadResult objects
            show_all: If True, show all jobs. If False, only show those with errors.
        """
        jobs_with_errors = [r for r in results if r.has_errors]

        print("\n" + "=" * 70)
        print("PROOFREADING RESULTS")
        print("=" * 70)
        print(f"Total jobs checked: {len(results)}")
        print(f"Jobs with errors: {len(jobs_with_errors)}")
        print("=" * 70)

        display_results = results if show_all else jobs_with_errors

        for result in display_results:
            if not result.original_text and not show_all:
                continue

            print(f"\n{'-' * 70}")
            print(f"Job ID: {result.job_id}")
            print(f"Job Name: {result.job_name}")
            print(f"Status: {'ERRORS FOUND' if result.has_errors else 'OK'}")

            if result.has_errors:
                print(f"\n--- ORIGINAL TEXT ---")
                print(result.original_text)

                print(f"\n--- CORRECTED TEXT ---")
                print(result.corrected_text)

                print(f"\n--- ERRORS ({len(result.errors)}) ---")
                for i, error in enumerate(result.errors, 1):
                    print(f"  {i}. {error['message']}")
                    if error['suggestions']:
                        print(f"     Suggestions: {', '.join(error['suggestions'])}")

            elif show_all and result.original_text:
                print(f"\n--- TEXT ---")
                print(result.original_text)

        print(f"\n{'=' * 70}")


def proofread_job_cards() -> list[ProofreadResult]:
    """
    Main function to fetch and proofread job cards.

    Returns:
        List of ProofreadResult objects
    """
    proofreader = Proofreader()
    results = proofreader.proofread_uninvoiced_jobs()
    proofreader.print_results(results)
    return results


if __name__ == "__main__":
    print("Job Card Proofreader")
    print("=" * 50)

    # Check if language tool is available
    if not LANGUAGE_TOOL_AVAILABLE:
        print("\nError: language_tool_python is not installed.")
        print("Run: pip install language-tool-python")
        exit(1)

    # Run proofreading
    results = proofread_job_cards()

    if not results:
        print("\nNo jobs to proofread. This could mean:")
        print("  1. API credentials are not configured")
        print("  2. No completed uninvoiced jobs exist")
        print("  3. API connection failed")
