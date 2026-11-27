import re


def slugify(text, max_len=None):
    """Lowercase ASCII slug whose words are joined by single hyphens.

    With max_len the slug is cut to at most max_len characters; it must never start or end with a hyphen.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_len is not None:
        slug = slug[:max_len]
    return slug
