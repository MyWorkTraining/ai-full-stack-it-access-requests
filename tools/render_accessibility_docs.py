"""Recreate the local HTML presentation used for the Firefox and Orca checks."""

import argparse
from html import escape
from pathlib import Path
from posixpath import relpath
import re

import markdown


ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    Path('README.md'),
    Path('deliverables/accuracy.md'),
    Path('deliverables/verification.md'),
    Path('planning/plan.md'),
    Path('planning/spec.md'),
)
STYLE = """
body {
    font: 1rem/1.55 system-ui, sans-serif;
    background: #fff;
    color: #111;
    margin: 1rem auto;
    max-width: 72rem;
    padding: 0 1rem;
}
a { color: #003399; }
:focus-visible { outline: 3px solid #003399; outline-offset: 2px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
code { overflow-wrap: anywhere; }
table { border-collapse: collapse; }
th, td { border: 1px solid #666; padding: .35rem; vertical-align: top; }
"""


def render_page(page, source, output_dir):
    text = source.read_text(encoding='utf-8')
    title = escape(text.splitlines()[0].removeprefix('# '))
    body = markdown.markdown(text, extensions=['tables', 'fenced_code', 'toc'])
    links = {
        relpath(target, page.parent): relpath(target.with_suffix('.html'), page.parent)
        for target in PAGES
    }

    def rewrite_link(match):
        href = match.group(1)
        path, separator, fragment = href.partition('#')
        return f' href="{links.get(path, path)}{separator}{fragment}"'

    body = re.sub(r' href="([^"]*)"', rewrite_link, body)
    destination = output_dir / page.with_suffix('.html')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<title>{title}</title>\n<style>{STYLE}</style>\n</head>\n'
        f'<body><main>{body}</main></body>\n</html>\n',
        encoding='utf-8',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--accuracy', type=Path, help='Use the accuracy report from a fresh replay.')
    args = parser.parse_args()

    for page in PAGES:
        source = ROOT / page
        if page == Path('deliverables/accuracy.md') and args.accuracy is not None:
            source = args.accuracy
        render_page(page, source, args.output_dir)
    print(args.output_dir.resolve() / 'README.html')


if __name__ == '__main__':
    main()
