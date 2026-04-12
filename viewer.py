"""
Simple web viewer for processed Almonte Gazette issues.

Generates a static HTML page that lists all processed issues
and links to overlays, column images, and ad images.

Regenerate after processing new issues:
    python3 viewer.py

Then browse at:
    https://mcmniintstdio.surfaceimpression.com/MVTM/viewer.html
"""

import os
import json
import glob


def generate_viewer(columns_dir="columns", output_path="viewer.html",
                    base_url="/MVTM"):
    """Generate the viewer HTML from processed issue directories."""

    # Find all issues with summaries
    issues = []
    for summary_path in sorted(glob.glob(f"{columns_dir}/*/issue_summary.json")):
        with open(summary_path) as f:
            summary = json.load(f)
        issue_dir = os.path.dirname(summary_path)
        issue_name = os.path.basename(issue_dir)

        # Count assets
        n_cols = 0
        n_ads = 0
        page_data = []

        for pg in summary.get("pages", []):
            pn = pg["page"]
            page_dir = os.path.join(issue_dir, f"p{pn}")

            # Count column files
            cols = sorted(glob.glob(f"{page_dir}/*_col*.png"))
            n_cols += len(cols)

            # Check overlay
            overlay = os.path.exists(os.path.join(page_dir, "overlay.png"))

            page_data.append({
                "page": pn,
                "num_columns": pg["num_columns"],
                "cv": pg["cv"],
                "widths": pg["widths"],
                "flags": pg.get("quality_flags", []),
                "has_overlay": overlay,
                "col_files": [os.path.basename(c) for c in cols],
            })

        # Count ads
        ads_dir = os.path.join(issue_dir, "ads")
        ad_data = []
        if os.path.exists(ads_dir):
            for ad_page_dir in sorted(glob.glob(f"{ads_dir}/p*")):
                pn = os.path.basename(ad_page_dir).replace("p", "")
                ad_files = sorted(glob.glob(f"{ad_page_dir}/*.png"))
                n_ads += len(ad_files)
                for af in ad_files:
                    ad_data.append({
                        "page": pn,
                        "file": os.path.basename(af),
                        "path": os.path.relpath(af, columns_dir),
                    })

        issues.append({
            "name": issue_name,
            "year": summary.get("year"),
            "month": summary.get("month"),
            "day": summary.get("day"),
            "pitch": summary.get("pitch"),
            "num_columns": summary.get("num_columns"),
            "elapsed": summary.get("elapsed"),
            "n_pages": len(page_data),
            "n_cols": n_cols,
            "n_ads": n_ads,
            "pages": page_data,
            "ads": ad_data,
            "dir": issue_name,
        })

    # Generate HTML
    html = []
    html.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Almonte Gazette — Processed Issues</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, system-ui, sans-serif; background: #f5f5f0; color: #333; padding: 20px; }
h1 { font-size: 1.8em; margin-bottom: 5px; }
.subtitle { color: #666; margin-bottom: 20px; }
.issue-list { display: grid; gap: 15px; margin-bottom: 30px; }
.issue-card { background: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; cursor: pointer; }
.issue-card:hover { border-color: #999; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
.issue-card h2 { font-size: 1.2em; margin-bottom: 5px; }
.issue-card .stats { color: #666; font-size: 0.9em; }
.issue-detail { display: none; background: white; border: 1px solid #ddd; border-radius: 8px; padding: 20px; margin-bottom: 30px; }
.issue-detail.active { display: block; }
.back-btn { display: inline-block; margin-bottom: 15px; padding: 5px 12px; background: #eee; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; text-decoration: none; color: #333; }
.page-section { margin-bottom: 20px; border-bottom: 1px solid #eee; padding-bottom: 15px; }
.page-section h3 { margin-bottom: 8px; }
.page-meta { font-size: 0.85em; color: #666; margin-bottom: 8px; }
.thumb-row { display: flex; flex-wrap: wrap; gap: 8px; }
.thumb { max-width: 120px; border: 1px solid #ddd; border-radius: 4px; }
.thumb-wide { max-width: 300px; }
.flag { display: inline-block; background: #fff3cd; color: #856404; font-size: 0.75em; padding: 1px 6px; border-radius: 3px; margin-right: 4px; }
.section-title { font-size: 1.1em; font-weight: 600; margin: 15px 0 8px; padding-top: 10px; border-top: 1px solid #eee; }
a img { transition: transform 0.15s; }
a img:hover { transform: scale(1.05); }
</style>
</head>
<body>
<h1>Almonte Gazette</h1>
<p class="subtitle">Processed Issues — Column Extraction Pipeline</p>
""")

    # Issue list
    html.append('<div class="issue-list" id="issue-list">')
    for issue in issues:
        date = f"{issue['year']}-{issue['month']:02d}-{issue['day']:02d}"
        html.append(f"""
<div class="issue-card" onclick="showIssue('{issue['name']}')">
  <h2>{date}</h2>
  <div class="stats">
    {issue['n_pages']} pages &middot;
    {issue['num_columns']} columns @ {issue['pitch']}% pitch &middot;
    {issue['n_cols']} column images &middot;
    {issue['n_ads']} ads &middot;
    {issue['elapsed']}s
  </div>
</div>""")
    html.append('</div>')

    # Issue detail panels
    for issue in issues:
        date = f"{issue['year']}-{issue['month']:02d}-{issue['day']:02d}"
        html.append(f"""
<div class="issue-detail" id="detail-{issue['name']}">
  <a class="back-btn" onclick="hideIssue('{issue['name']}')">&larr; Back to list</a>
  <h2>{date}</h2>
  <p class="stats" style="margin-bottom:15px">
    {issue['num_columns']} columns @ {issue['pitch']}% pitch &middot;
    {issue['n_cols']} column images &middot;
    {issue['n_ads']} ads
  </p>
""")

        # Pages
        for pg in issue["pages"]:
            pn = pg["page"]
            cv_str = f"CV={pg['cv']:.3f}" if pg["cv"] < 900 else "failed"
            widths_str = " ".join(f"{w:.0f}%" for w in pg["widths"])
            flags_html = "".join(f'<span class="flag">{f}</span>'
                                for f in pg["flags"]
                                if "anchor" in f or "prior" in f or "sliver" in f)

            html.append(f"""
  <div class="page-section">
    <h3>Page {pn}</h3>
    <div class="page-meta">
      {pg['num_columns']} cols [{widths_str}] {cv_str} {flags_html}
    </div>""")

            # Overlay
            if pg["has_overlay"]:
                overlay_url = f"{base_url}/columns/{issue['dir']}/p{pn}/overlay.png"
                html.append(f"""
    <a href="{overlay_url}" target="_blank">
      <img class="thumb thumb-wide" src="{overlay_url}" alt="Page {pn} overlay" loading="lazy">
    </a>""")

            # Column thumbnails
            if pg["col_files"]:
                html.append('    <div class="thumb-row" style="margin-top:8px">')
                for cf in pg["col_files"]:
                    col_url = f"{base_url}/columns/{issue['dir']}/p{pn}/{cf}"
                    html.append(f"""
      <a href="{col_url}" target="_blank">
        <img class="thumb" src="{col_url}" alt="{cf}" loading="lazy">
      </a>""")
                html.append("    </div>")

            html.append("  </div>")

        # Ads section
        if issue["ads"]:
            html.append('  <div class="section-title">Display Ads</div>')
            html.append('  <div class="thumb-row">')
            for ad in issue["ads"]:
                ad_url = f"{base_url}/columns/{ad['path']}"
                html.append(f"""
    <a href="{ad_url}" target="_blank">
      <img class="thumb" src="{ad_url}" alt="P{ad['page']} {ad['file']}" loading="lazy">
    </a>""")
            html.append("  </div>")

        html.append("</div>")

    # JavaScript
    html.append("""
<script>
function showIssue(name) {
  document.getElementById('issue-list').style.display = 'none';
  document.getElementById('detail-' + name).classList.add('active');
}
function hideIssue(name) {
  document.getElementById('detail-' + name).classList.remove('active');
  document.getElementById('issue-list').style.display = 'grid';
}
</script>
</body>
</html>
""")

    with open(output_path, "w") as f:
        f.write("\n".join(html))

    print(f"Viewer generated: {output_path}")
    print(f"  {len(issues)} issues")
    print(f"  {sum(i['n_cols'] for i in issues)} column images")
    print(f"  {sum(i['n_ads'] for i in issues)} ads")


if __name__ == "__main__":
    generate_viewer()
