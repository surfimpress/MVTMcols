"""
Layout intelligence layer for the Almonte Gazette pipeline.

Accumulates knowledge about column layouts across pages and issues,
building era-level patterns that improve detection accuracy over time.

Key concepts:
- A "layout" is a column grid: the number of columns and their
  approximate widths as percentages of page width.
- Layouts are stored per-page after successful detection.
- Era patterns emerge by aggregating layouts across nearby years.
- When processing a new page, the era pattern acts as a prior:
  it biases detection toward the expected column count and widths.

Storage: all data lives in the existing mvtm.db SQLite database.

Usage:
    from layout_intelligence import LayoutDB

    db = LayoutDB("data/mvtm.db")

    # After successful column detection:
    db.record_layout(year=1920, month=1, day=2, page=3,
                     num_columns=7, boundary_positions=[14.2, 25.1, ...],
                     quality_flags=[], confidence=0.85)

    # Before detecting columns on a new page:
    prior = db.get_prior(year=1920)
    # prior = {"expected_columns": 7, "typical_widths": [11.2, 11.0, ...], ...}
"""

import sqlite3
import json
import numpy as np
from collections import Counter


class LayoutDB:
    """Interface to the layout intelligence tables in SQLite."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._init_tables()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_tables(self):
        """Create intelligence tables if they don't exist."""
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS page_layouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER,
                day INTEGER,
                page INTEGER,
                num_columns INTEGER NOT NULL,
                boundary_positions TEXT NOT NULL,
                column_widths TEXT NOT NULL,
                quality_flags TEXT,
                confidence REAL,
                profile_json TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS page_geometry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER,
                day INTEGER,
                page INTEGER,
                r2_left REAL, r2_right REAL,
                r3_left REAL, r3_right REAL,
                text_left REAL, text_right REAL,
                binding_side TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_page_geometry_year
                ON page_geometry(year);

            CREATE INDEX IF NOT EXISTS idx_page_geometry_issue
                ON page_geometry(year, month, day);

            CREATE TABLE IF NOT EXISTS layout_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_name TEXT NOT NULL,
                page_number INTEGER,
                year_start INTEGER NOT NULL,
                year_end INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(template_name, page_number, year_start, year_end)
            );

            CREATE INDEX IF NOT EXISTS idx_page_layouts_year
                ON page_layouts(year);

            CREATE INDEX IF NOT EXISTS idx_page_layouts_issue
                ON page_layouts(year, month, day);

            CREATE TABLE IF NOT EXISTS era_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year_start INTEGER NOT NULL,
                year_end INTEGER NOT NULL,
                dominant_columns INTEGER NOT NULL,
                typical_widths TEXT,
                sample_count INTEGER,
                confidence REAL,
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(year_start, year_end)
            );
        """)
        conn.commit()
        conn.close()

    # ── Recording results ────────────────────────────────────────────

    def record_layout(self, year, month, day, page, num_columns,
                      boundary_positions, quality_flags=None,
                      confidence=None, profile=None):
        """
        Store a detected layout for a page.

        Args:
            year, month, day, page: issue identification
            num_columns: detected column count
            boundary_positions: list of boundary x_pct values
            quality_flags: list of flag strings from detection
            confidence: 0-1 overall confidence score
            profile: dict from page_profile (optional, stored as JSON)
        """
        # Compute column widths from boundary positions.
        # Boundaries should include left edge of first column and
        # right edge of last column — NOT padded with 0/100.
        edges = list(boundary_positions)
        widths = [round(edges[i+1] - edges[i], 2) for i in range(len(edges)-1)]

        conn = self._conn()
        conn.execute("""
            INSERT INTO page_layouts
            (year, month, day, page, num_columns, boundary_positions,
             column_widths, quality_flags, confidence, profile_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            year, month, day, page, num_columns,
            json.dumps([round(p, 2) for p in boundary_positions]),
            json.dumps(widths),
            json.dumps(quality_flags or []),
            confidence,
            json.dumps(profile) if profile else None,
        ))
        conn.commit()
        conn.close()

    def record_template(self, template_name, page_number, year_start,
                        year_end, pattern, description=None):
        """
        Store a recurring layout template.

        Args:
            template_name:  e.g. "page2_editorial_wide"
            page_number:    Which page this applies to (e.g. 2)
            year_start:     First year the pattern was observed
            year_end:       Last year the pattern was observed
            pattern:        JSON-serialisable dict describing the pattern
            description:    Human-readable description
        """
        conn = self._conn()
        conn.execute("""
            INSERT OR REPLACE INTO layout_templates
            (template_name, page_number, year_start, year_end,
             pattern, description)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            template_name, page_number, year_start, year_end,
            json.dumps(pattern), description,
        ))
        conn.commit()
        conn.close()

    def get_template(self, template_name, page_number, year, window=10):
        """
        Get a layout template for a specific page and year.

        First checks for an exact year match, then looks within
        ±window years for a nearby template (layout patterns persist
        for spans of years).

        Returns the pattern dict if found, None otherwise.
        """
        conn = self._conn()

        # Exact match first
        row = conn.execute("""
            SELECT pattern, year_start, year_end, description
            FROM layout_templates
            WHERE template_name = ? AND page_number = ?
              AND year_start <= ? AND year_end >= ?
        """, (template_name, page_number, year, year)).fetchone()

        if not row:
            # Look for nearest template within window
            row = conn.execute("""
                SELECT pattern, year_start, year_end, description
                FROM layout_templates
                WHERE template_name = ? AND page_number = ?
                  AND year_start <= ? + ? AND year_end >= ? - ?
                ORDER BY ABS(year_start + year_end - ? * 2)
                LIMIT 1
            """, (template_name, page_number, year, window,
                  year, window, year)).fetchone()

        conn.close()

        if row:
            return {
                "pattern": json.loads(row[0]),
                "year_start": row[1],
                "year_end": row[2],
                "description": row[3],
            }
        return None

    def update_template_range(self, template_name, page_number, year):
        """
        Extend an existing template's year range to include a new year.

        Finds the nearest template and extends its range to cover
        the new year, bridging any gap.
        """
        conn = self._conn()
        # Find the nearest template
        row = conn.execute("""
            SELECT id, year_start, year_end FROM layout_templates
            WHERE template_name = ? AND page_number = ?
            ORDER BY ABS(year_start + year_end - ? * 2)
            LIMIT 1
        """, (template_name, page_number, year)).fetchone()

        if row:
            new_start = min(row[1], year)
            new_end = max(row[2], year)
            conn.execute("""
                UPDATE layout_templates
                SET year_start = ?, year_end = ?
                WHERE id = ?
            """, (new_start, new_end, row[0]))
            conn.commit()
        conn.close()

    def record_geometry(self, year, month, day, page, profile):
        """
        Store the page's bounding box geometry from the profile.

        Args:
            year, month, day, page: issue identification
            profile: dict from profile_page() containing r2, r3, text_area
        """
        if not profile or "r2" not in profile:
            return

        conn = self._conn()
        conn.execute("""
            INSERT INTO page_geometry
            (year, month, day, page,
             r2_left, r2_right, r3_left, r3_right,
             text_left, text_right, binding_side)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            year, month, day, page,
            profile["r2"]["left"], profile["r2"]["right"],
            profile["r3"]["left"], profile["r3"]["right"],
            profile["text_area"]["left"], profile["text_area"]["right"],
            profile.get("binding_side"),
        ))
        conn.commit()
        conn.close()

    def record_from_page_result(self, page_result, year, month, day, page,
                                profile=None):
        """
        Convenience: record layout and geometry from a split_page result.
        """
        if page_result.error:
            return

        # Store geometry if profile provided
        if profile:
            self.record_geometry(year, month, day, page, profile)

        # Store layout
        boundaries = []
        for c in page_result.columns:
            if c.right_vw < 100:
                boundaries.append(c.right_vw)

        # Confidence: fraction of high/medium confidence columns
        conf_scores = {"high": 1.0, "medium": 0.7, "low": 0.3, "n/a": 0.5}
        if page_result.columns:
            conf = np.mean([conf_scores.get(c.confidence, 0.5)
                           for c in page_result.columns])
        else:
            conf = 0.0

        self.record_layout(
            year, month, day, page,
            num_columns=page_result.num_columns,
            boundary_positions=boundaries,
            quality_flags=page_result.quality_flags,
            confidence=round(float(conf), 3),
        )

    # ── Querying priors ──────────────────────────────────────────────

    def get_prior(self, year, window=5):
        """
        Get layout expectations for a given year based on accumulated data.

        Looks at pages within ±window years. Returns None if no data.

        Args:
            year: the target year
            window: how many years on each side to consider

        Returns:
            dict with expected_columns, typical_widths, sample_count,
            confidence, or None if no data.
        """
        conn = self._conn()
        rows = conn.execute("""
            SELECT num_columns, column_widths, confidence
            FROM page_layouts
            WHERE year BETWEEN ? AND ?
              AND confidence > 0.3
        """, (year - window, year + window)).fetchall()
        conn.close()

        if not rows:
            return None

        # Find dominant column count
        col_counts = Counter(r[0] for r in rows)
        dominant = col_counts.most_common(1)[0][0]

        # Collect widths from pages matching the dominant count
        matching_widths = []
        matching_confs = []
        for num_cols, widths_json, conf in rows:
            if num_cols == dominant:
                matching_widths.append(json.loads(widths_json))
                matching_confs.append(conf or 0.5)

        # Average the widths (they should be similar for the same column count)
        if matching_widths:
            # Pad shorter lists and truncate longer ones to dominant count
            padded = []
            for w in matching_widths:
                if len(w) == dominant:
                    padded.append(w)
            if padded:
                avg_widths = np.mean(padded, axis=0).tolist()
                avg_widths = [round(w, 1) for w in avg_widths]
            else:
                avg_widths = None
        else:
            avg_widths = None

        return {
            "expected_columns": dominant,
            "typical_widths": avg_widths,
            "sample_count": len(rows),
            "matching_count": len(matching_widths),
            "confidence": round(float(np.mean(matching_confs)), 3),
            "column_count_distribution": dict(col_counts),
            "year_range": (year - window, year + window),
        }

    def get_issue_prior(self, year, month, day):
        """
        Get layout from other pages in the same issue.
        Within an issue, the column grid is constant (though content varies).
        """
        conn = self._conn()
        rows = conn.execute("""
            SELECT num_columns, column_widths, confidence, page
            FROM page_layouts
            WHERE year = ? AND month = ? AND day = ?
            ORDER BY confidence DESC
        """, (year, month, day)).fetchall()
        conn.close()

        if not rows:
            return None

        # Use the highest-confidence page's layout
        best = rows[0]
        return {
            "expected_columns": best[0],
            "typical_widths": json.loads(best[1]),
            "source_page": best[3],
            "confidence": best[2],
            "pages_processed": len(rows),
        }

    # ── Era pattern computation ──────────────────────────────────────

    def compute_era_patterns(self, bin_size=10):
        """
        Aggregate page_layouts into era_patterns.

        Groups years into bins (default 10-year) and finds the
        dominant column count and typical widths for each era.
        """
        conn = self._conn()
        rows = conn.execute("""
            SELECT year, num_columns, column_widths, confidence
            FROM page_layouts
            WHERE confidence > 0.3
            ORDER BY year
        """).fetchall()
        conn.close()

        if not rows:
            return []

        # Group by decade
        year_min = min(r[0] for r in rows)
        year_max = max(r[0] for r in rows)

        patterns = []
        for era_start in range(year_min - (year_min % bin_size),
                               year_max + bin_size, bin_size):
            era_end = era_start + bin_size - 1
            era_rows = [r for r in rows if era_start <= r[0] <= era_end]

            if not era_rows:
                continue

            col_counts = Counter(r[1] for r in era_rows)
            dominant = col_counts.most_common(1)[0][0]

            # Average widths for dominant column count
            matching = [json.loads(r[2]) for r in era_rows
                       if r[1] == dominant and len(json.loads(r[2])) == dominant]

            if matching:
                avg_widths = np.mean(matching, axis=0).tolist()
                avg_widths = [round(w, 1) for w in avg_widths]
            else:
                avg_widths = None

            avg_conf = np.mean([r[3] or 0.5 for r in era_rows
                               if r[1] == dominant])

            patterns.append({
                "year_start": era_start,
                "year_end": era_end,
                "dominant_columns": dominant,
                "typical_widths": avg_widths,
                "sample_count": len(era_rows),
                "confidence": round(float(avg_conf), 3),
            })

        # Store in database
        conn = self._conn()
        for p in patterns:
            conn.execute("""
                INSERT OR REPLACE INTO era_patterns
                (year_start, year_end, dominant_columns, typical_widths,
                 sample_count, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                p["year_start"], p["year_end"],
                p["dominant_columns"],
                json.dumps(p["typical_widths"]),
                p["sample_count"],
                p["confidence"],
            ))
        conn.commit()
        conn.close()

        return patterns

    # ── Reporting ────────────────────────────────────────────────────

    def summary(self):
        """Print a summary of accumulated intelligence."""
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM page_layouts").fetchone()[0]
        years = conn.execute(
            "SELECT MIN(year), MAX(year) FROM page_layouts"
        ).fetchone()

        if total == 0:
            print("No layout data recorded yet.")
            conn.close()
            return

        print(f"Layout intelligence: {total} pages, {years[0]}-{years[1]}")

        # Column count distribution
        dist = conn.execute("""
            SELECT num_columns, COUNT(*) as cnt
            FROM page_layouts
            GROUP BY num_columns
            ORDER BY cnt DESC
        """).fetchall()
        print(f"  Column counts: " +
              ", ".join(f"{n}cols={c}" for n, c in dist))

        # Era patterns
        eras = conn.execute("""
            SELECT year_start, year_end, dominant_columns, sample_count
            FROM era_patterns
            ORDER BY year_start
        """).fetchall()
        if eras:
            print(f"  Era patterns ({len(eras)} decades):")
            for start, end, cols, samples in eras:
                print(f"    {start}-{end}: {cols} columns ({samples} samples)")

        conn.close()


def print_prior(prior):
    """Pretty-print a layout prior."""
    if prior is None:
        print("  No prior data available.")
        return
    print(f"  Expected columns: {prior['expected_columns']}")
    if prior.get("typical_widths"):
        widths_str = " ".join(f"{w:.0f}%" for w in prior["typical_widths"])
        print(f"  Typical widths: [{widths_str}]")
    print(f"  Based on {prior.get('sample_count', prior.get('pages_processed', 0))} samples, "
          f"confidence={prior['confidence']:.2f}")
    if prior.get("column_count_distribution"):
        print(f"  Distribution: {prior['column_count_distribution']}")


if __name__ == "__main__":
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/mvtm.db"
    db = LayoutDB(db_path)
    db.summary()

    if len(sys.argv) > 2:
        year = int(sys.argv[2])
        print(f"\nPrior for {year}:")
        prior = db.get_prior(year)
        print_prior(prior)
