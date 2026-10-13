import os
import re
import markdown
from flask import Blueprint, render_template, abort, send_from_directory, request

# The wiki is deprecated to a Discord-staff-only route. Uses the staff allowlist
# ([leaderboard] staff_discord_ids); open in 'Development' site_type for local testing.
from staff_auth import staff_required

bp = Blueprint('wiki', __name__, template_folder='templates')

WIKI_DIR = '/app/nms-wiki'
if not os.path.exists(WIKI_DIR):
    # Fallback for local development
    WIKI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'nms-wiki'))

WIKI_SECTIONS = [
    {
        "title": "Getting Started",
        "items": [
            {"title": "Welcome / Home", "path": "home"},
            {"title": "Getting Started", "path": "getting-started"},
            {"title": "FAQs", "path": "FAQs"},
            {"title": "Rules", "path": "rules"},
            {"title": "Live vs NMS", "path": "Live-vs-NMS"},
        ]
    },
    {
        "title": "Character & Classes",
        "items": [
            {"title": "Classes & Abilities", "path": "classes-and-abilities"},
            {"title": "Progression Guide", "path": "progression"},
            {"title": "Log Parser", "path": "Log-Parser"},
        ]
    },
    {
        "title": "World Guides",
        "items": [
            {"title": "Expansion Guide", "path": "expansion-guide"},
            {"title": "Exploration & Combat", "path": "exploration-and-combat"},
            {"title": "Equipment Guide", "path": "equipment-guide"},
            {"title": "Quests", "path": "quests"},
        ]
    },
    {
        "title": "Changelogs",
        "items": [
            {"title": "Changelog History", "path": "Change-log-history"},
            {"title": "June 2025", "path": "changelog-2025-06"},
            {"title": "May 2025", "path": "changelog-2025-05"},
            {"title": "April 2025", "path": "changelog-2025-04"},
            {"title": "March 2025", "path": "changelog-2025-03"},
            {"title": "February 2025", "path": "changelog-2025-02"},
            {"title": "January 2025", "path": "changelog-2025-01"},
            {"title": "December 2024", "path": "changelog-2024-12"},
            {"title": "November 2024", "path": "changelog-2024-11"},
            {"title": "October 2024", "path": "changelog-2024-10"},
            {"title": "September 2024", "path": "changelog-2024-09"},
            {"title": "August 2024", "path": "changelog-2024-08"},
            {"title": "July 2024", "path": "changelog-2024-07"},
            {"title": "June 2024", "path": "changelog-2024-06"},
            {"title": "May 2024", "path": "changelog-2024-05"},
            {"title": "April 2024", "path": "changelog-2024-04"},
            {"title": "March 2024", "path": "changelog-2024-03"},
            {"title": "February 2024", "path": "changelog-2024-02"},
        ]
    }
]


def convert_markdown_links(html, current_dir=""):
    pattern = r'href="((?!http|https)[^"]+)"'
    
    def replace_link(match):
        link = match.group(1)
        if link.startswith('/wiki/') or link.startswith('/wiki'):
            return match.group(0)
            
        if link == '/':
            return match.group(0)
            
        anchor = ""
        if '#' in link:
            parts = link.split('#', 1)
            link = parts[0]
            anchor = '#' + parts[1]
            
        is_md = link.endswith('.md')
        is_html = link.endswith('.html')
        
        if link.startswith('/'):
            clean_link = link.lstrip('/')
            if clean_link.endswith(".md"):
                clean_link = clean_link[:-3]
            elif clean_link.endswith(".html"):
                clean_link = clean_link[:-5]
            return f'href="/wiki/{clean_link}{anchor}"'
            
        if is_md or is_html:
            clean_link = link[:-3] if is_md else link[:-5]
            if current_dir:
                full_path = os.path.normpath(os.path.join(current_dir, clean_link))
            else:
                full_path = os.path.normpath(clean_link)
                
            full_path = full_path.replace('\\', '/')
            if full_path.startswith('.'):
                full_path = full_path.lstrip('./')
                
            return f'href="/wiki/{full_path}{anchor}"'
            
        return match.group(0)
        
    return re.sub(pattern, replace_link, html)


def convert_image_src(html, current_dir=""):
    pattern = r'src="((?!http|https)[^"]+)"'
    
    def replace_src(match):
        src = match.group(1)
        if src.startswith('/wiki/media/'):
            return match.group(0)
            
        if src.startswith('/'):
            clean_src = src.lstrip('/')
            return f'src="/wiki/media/{clean_src}"'
            
        if current_dir:
            full_path = os.path.normpath(os.path.join(current_dir, src))
        else:
            full_path = os.path.normpath(src)
            
        full_path = full_path.replace('\\', '/')
        if full_path.startswith('.'):
            full_path = full_path.lstrip('./')
            
        return f'src="/wiki/media/{full_path}"'
        
    return re.sub(pattern, replace_src, html)


@bp.route("/wiki", defaults={"page_path": "home"}, methods=["GET"])
@bp.route("/wiki/<path:page_path>", methods=["GET"])
@staff_required
def view_wiki(page_path):
    # Security check: prevent directory traversal
    normalized_path = os.path.normpath(page_path)
    if normalized_path.startswith("..") or os.path.isabs(normalized_path):
        abort(404)
        
    # Resolve current directory of the page (for link/image mapping)
    current_dir = os.path.dirname(normalized_path)
    
    # Check for markdown first
    md_file = os.path.join(WIKI_DIR, normalized_path + ".md")
    html_file = os.path.join(WIKI_DIR, normalized_path + ".html")
    
    content_html = None
    page_title = os.path.basename(normalized_path).replace("-", " ").title()
    
    if os.path.exists(md_file):
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                content = f.read()
            raw_html = markdown.markdown(content, extensions=['tables', 'fenced_code', 'toc'])
            raw_html = convert_markdown_links(raw_html, current_dir)
            content_html = convert_image_src(raw_html, current_dir)
        except Exception as e:
            abort(500)
    elif os.path.exists(html_file):
        try:
            with open(html_file, "r", encoding="utf-8") as f:
                content = f.read()
            content = convert_markdown_links(content, current_dir)
            content_html = convert_image_src(content, current_dir)
        except Exception as e:
            abort(500)
    else:
        # Fallback: if it's a directory, look for index.md/README.md
        dir_path = os.path.join(WIKI_DIR, normalized_path)
        if os.path.isdir(dir_path):
            index_md = os.path.join(dir_path, "home.md")
            if not os.path.exists(index_md):
                index_md = os.path.join(dir_path, "README.md")
            if os.path.exists(index_md):
                try:
                    with open(index_md, "r", encoding="utf-8") as f:
                        content = f.read()
                    raw_html = markdown.markdown(content, extensions=['tables', 'fenced_code', 'toc'])
                    raw_html = convert_markdown_links(raw_html, normalized_path)
                    content_html = convert_image_src(raw_html, normalized_path)
                except Exception as e:
                    abort(500)
                    
    if content_html is None:
        abort(404)
        
    return render_template(
        "wiki_page.html",
        content=content_html,
        sections=WIKI_SECTIONS,
        active_path=page_path,
        page_title=page_title
    )


@bp.route("/wiki/media/<path:filename>")
@staff_required
def wiki_media(filename):
    # Security check: prevent directory traversal
    normalized_path = os.path.normpath(filename)
    if normalized_path.startswith("..") or os.path.isabs(normalized_path):
        abort(404)
        
    return send_from_directory(WIKI_DIR, filename)
