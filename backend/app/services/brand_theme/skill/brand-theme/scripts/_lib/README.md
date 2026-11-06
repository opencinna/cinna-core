# Support code for the brand-theme scripts (not run directly)

- `brand_theme/`: a verbatim copy of Cinna core's validator modules
  (`backend/app/services/brand_theme/` in cinna-core: schema, colors, images,
  svg_sanitizer, validator, package), without the `.py` extension. Core's test
  suite fails when the copy and the originals differ. Do not edit these files
  here; change core and copy them again.
- `skill_lib`: loads that copy as the package `cinna_brand_theme` and holds the
  helpers the four scripts share.

No file in this skill ends in `.py`: a Cinna core holding a published skill
restarts when one does.
