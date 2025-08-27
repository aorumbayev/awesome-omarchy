# Contributing to Awesome Omarchy

Quick guide for contributing to this curated list of Omarchy resources.

## What to Include

✅ **Yes:** High-quality Omarchy themes, tools, guides, and related projects (5+ stars, actively maintained)
❌ **No:** Broken links, duplicates, unrelated content, or low-quality resources

## How to Contribute

1. Fork → Create branch → Add resource → Submit PR
2. Use format: `- [Name](link) - Brief description.`
3. Add to appropriate section (Official, Alternative, Themes, Tools, Related, Community)
4. Ensure links work and descriptions end with periods

## Local Validation

**Setup:**
```bash
pip install pre-commit && pre-commit install
# No additional setup needed - all tools use npx
```

**Usage:**
```bash
pre-commit run --all-files  # Run all checks
pre-commit run typos        # Check spelling
```

This runs the same tools as CI: awesome-lint, markdown formatting, spell check, and link validation.

## Requirements

**All Resources:**
- 5+ GitHub stars (exceptions for official/essential resources)
- Active maintenance (recent commits/releases)
- Working links and clear documentation
- Relevant to Omarchy ecosystem

**Descriptions:**
- Concise (1 sentence)
- Start with capital, end with period
- Describe function, not marketing

**Example:**
```markdown
- [Midnight Theme](https://github.com/JaxonWright/omarchy-midnight-theme) - Dark theme optimized for OLED displays.
```

Questions? [Open an issue](https://github.com/aorumbayev/awesome-omarchy/issues)
