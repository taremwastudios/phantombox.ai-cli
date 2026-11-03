"""Preview branding output as text, with display widths, to check the logo."""
import sys
sys.path.insert(0, "src")

from phantombox.branding import banner_lines, _wordmark, _MARK_FILLED

try:
    from wcwidth import wcswidth
except Exception:
    def wcswidth(s):
        return sum(2 if ord(c) > 0x2E7F else 1 for c in s)

print("--- wordmark rows ---")
for r in _wordmark():
    print(f"|{r}|  disp={wcswidth(r)}")
print()
print("--- full banner ---")
for r in banner_lines():
    print(f"|{r}|  disp={wcswidth(r)}")
print()
print("max display width:", max(wcswidth(r) for r in banner_lines()))
