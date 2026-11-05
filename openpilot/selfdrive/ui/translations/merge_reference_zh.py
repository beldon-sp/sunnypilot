#!/usr/bin/env python3
"""Adopt zh translations from the reference fork (以参考为准、我们补充).

Provenance rules for app_zh-CHS.po / app_zh-CHT.po:
- msgids shared with the reference repo (mouxangithub/openpilot) keep the
  reference translation;
- msgids that only exist in this tree (BYD etc.) keep our existing translation;
- msgids that only exist in the reference repo (carrot-only features) are
  dropped, never merged in.

Mechanics:
1. snapshot our current zh catalogs;
2. copy the reference catalogs over ours;
3. regenerate the pot from this tree and merge it into every language, which
   applies the two rules structurally (exact-match merge keeps reference
   translations for shared msgids, drops reference-only msgids, and adds
   this-tree-only msgids untranslated);
4. fill the entries still untranslated in zh from the snapshot (我们补充).

Usage: python3 selfdrive/ui/translations/merge_reference_zh.py <reference-repo-root>
"""
import os
import shutil
import sys

BASEDIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
sys.path.insert(0, BASEDIR)

from openpilot.selfdrive.ui.translations.potools import parse_po, write_po
from openpilot.selfdrive.ui.translations.update_translations import update_translations

LANGS = ("zh-CHS", "zh-CHT")
TRANSLATIONS_DIR = os.path.join(BASEDIR, "openpilot", "selfdrive", "ui", "translations")


def translation_map(path: str) -> dict:
  _, entries = parse_po(path)
  translations = {}
  for e in entries:
    if e.msgstr:
      translations[e.msgid] = e.msgstr
    for idx, s in e.msgstr_plural.items():
      if s:
        translations[(e.msgid, idx)] = s
  return translations


def fill_untranslated(po_path: str, backup: dict) -> tuple[int, int]:
  header, entries = parse_po(po_path)
  filled = 0
  total = 0
  for e in entries:
    total += 1
    if not e.msgstr:
      ours = backup.get(e.msgid)
      if ours:
        e.msgstr = ours
        filled += 1
    for idx in list(e.msgstr_plural):
      if not e.msgstr_plural[idx]:
        ours = backup.get((e.msgid, idx))
        if ours:
          e.msgstr_plural[idx] = ours
          filled += 1
  write_po(po_path, header, entries)
  return filled, total


def main() -> int:
  ref_root = os.path.abspath(sys.argv[1])
  ref_translations_dir = os.path.join(ref_root, "openpilot", "selfdrive", "ui", "translations")
  if not os.path.isdir(ref_translations_dir):
    print(f"reference translations dir not found: {ref_translations_dir}", file=sys.stderr)
    return 1

  backups = {}
  for lang in LANGS:
    ours_path = os.path.join(TRANSLATIONS_DIR, f"app_{lang}.po")
    ref_path = os.path.join(ref_translations_dir, f"app_{lang}.po")
    if not os.path.exists(ref_path):
      print(f"reference catalog missing: {ref_path}", file=sys.stderr)
      return 1
    backups[lang] = translation_map(ours_path)
    shutil.copyfile(ref_path, ours_path)
    print(f"{lang}: snapshot {len(backups[lang])} of our translations, adopted reference catalog")

  # Regenerate the pot from this tree and merge into every language. For the zh
  # catalogs (now holding the reference translations) this keeps shared msgids
  # with the reference translation, drops reference-only msgids, and adds
  # this-tree-only msgids untranslated.
  update_translations()

  for lang in LANGS:
    po_path = os.path.join(TRANSLATIONS_DIR, f"app_{lang}.po")
    _, entries = parse_po(po_path)
    translated = sum(1 for e in entries if e.msgstr or any(e.msgstr_plural.values()))
    filled, total = fill_untranslated(po_path, backups[lang])
    _, entries_after = parse_po(po_path)
    still_empty = sum(1 for e in entries_after if not e.msgstr and not any(e.msgstr_plural.values()))
    print(f"{lang}: {total} entries, {translated} from reference before fill, {filled} filled from our snapshot, {still_empty} left untranslated")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
