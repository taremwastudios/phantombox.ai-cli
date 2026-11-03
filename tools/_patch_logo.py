import io

p = "src/phantombox/branding.py"
src = io.open(p, encoding="utf-8").read()
lines = src.splitlines(keepends=True)

start = next(i for i, l in enumerate(lines) if l.startswith("_MARK_FILLED = ["))
end = start
while not lines[end].rstrip().endswith("]"):
    end += 1

new_mark = '''_MARK_FILLED = [
    "            ████████████████                 ╲ │ ╱   ",
    "          ██▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓██               ─ + ─   ",
    "        ██▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓██             ╱ │ ╲   ",
    "      ██▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓██                   ",
    "      ██████████████████████████████            ▄▄▄  ",
    "      ████████████████████▓▓▓▓▓▓▓▓▒▒▒▒         █   █ ",
    "      ████████████████████▓▓▓▓▓▓▓▓▒▒▒▒         █    █",
    "      ████████████████████▓▓▓▓▓▓▓▓▒▒▒▒         █▄▄▄▄█",
    "      ████████████████████▓▓▓▓▓▓▓▓▒▒▒▒                ",
    "      ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▓▓▓▓▓▓▓▓▒▒▒▒                ",
]
'''
lines[start:end + 1] = [new_mark]
src = "".join(lines)

old_shadow = '''    # Drop shadow: one row below, shifted right, only under the bottom edge.
    base = rows[-1]
    shadow = " " + "".join("\u2592" if base[i] != " " else " " for i in range(len(base))).rstrip()
    return rows + [shadow]'''

new_shadow = '''    # Drop shadow: the wordmark's whole silhouette translated one row down and
    # one cell right. Using the union of every row (not just the last one) makes
    # descenders such as the bowl of 'p' cast a continuous shadow instead of the
    # patchy fragments a bottom-row-only copy produced.
    silhouette = "".join(
        "\u2592" if any(r[i] != " " for r in rows if i < len(r)) else " "
        for i in range(width)
    )
    shadow = " " + silhouette.rstrip()
    return [r.rstrip() for r in rows] + [shadow]'''

assert old_shadow in src, "shadow block not found"
src = src.replace(old_shadow, new_shadow)

io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("patched OK")
