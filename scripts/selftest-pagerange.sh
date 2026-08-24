#!/bin/bash -e
#
# Offline self-test for the converter's page-range mode. Like the main suite it
# runs without a printer, without the network, and without installing anything.
#
#   ./scripts/selftest-pagerange.sh
#
# What is being tested is not really "does -dFirstPage work" -- Ghostscript can
# be trusted with that -- but the two places where a range makes an existing
# behaviour wrong. A converter that answers a request for pages 40-60 with the
# whole document would print the entire job in the middle of itself, and a
# converter that quietly returns a blank page for a range that selects nothing
# would put a blank sheet there instead. Both are worse than an error, so most
# of what follows checks that nothing at all comes back.
#
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$(readlink -f "$0")")/.."

pass=0
fail=0

ok()   { printf '  ok    %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

if ! command -v gs >/dev/null 2>&1; then
  echo '  skip  page ranges (ghostscript not installed)'
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM

# Test documents are built here rather than committed. A binary fixture in the
# tree would go stale against the Ghostscript that is actually installed, and
# the one thing every check below depends on is that the input really is what
# this file says it is.
#
# Every page is given its own width. Outlining removes the text, so after a
# conversion there is nothing left to search for to tell page 4 from page 5 --
# but the page geometry survives, and that is enough to say exactly which pages
# came back and in what order.
pagesource() {   # $1 = page count, $2 = PostScript file to write
  : > "$2"
  n=1
  while [ "$n" -le "$1" ]; do
    printf '<</PageSize [%d 400]>> setpagedevice\n' "$((200 + n))" >> "$2"
    printf '/Helvetica 24 selectfont 20 200 moveto (Page %d) show showpage\n' \
           "$n" >> "$2"
    n=$((n + 1))
  done
}

topdf() {   # $1 = PostScript file, $2 = PDF to write
  gs -q -dNOPAUSE -dBATCH -dSAFER -sDEVICE=pdfwrite -dEmbedAllFonts=true \
     -sOutputFile="$2" "$1" >/dev/null 2>&1
}

for count in 1 2 9; do
  pagesource "$count" "$work/p$count.ps"
  topdf "$work/p$count.ps" "$work/p$count.pdf"
done

# The same nine pages, with a gradient on page one and nowhere else. This is
# what makes the "a class of drawing construct went missing" check fire for a
# range that does not contain page one -- see the fail-safe section below.
{
  printf '<</PageSize [201 400]>> setpagedevice\n'
  printf '<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 150 300]\n'
  printf '   /Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0]\n'
  printf '                /C1 [0 0 1] /N 1 >> >> shfill\n'
  printf '/Helvetica 24 selectfont 20 200 moveto (Page 1) show showpage\n'
  n=2
  while [ "$n" -le 9 ]; do
    printf '<</PageSize [%d 400]>> setpagedevice\n' "$((200 + n))"
    printf '/Helvetica 24 selectfont 20 200 moveto (Page %d) show showpage\n' "$n"
    n=$((n + 1))
  done
} > "$work/shade.ps"
topdf "$work/shade.ps" "$work/shade.pdf"

printf 'UNIRAST\0not a pdf at all' > "$work/raster.bin"

# Ghostscript's own page count, which is the number the converter reports and
# the number -dFirstPage counts against. Asking it the same way the converter
# does is deliberate: if the two ever disagree the answer is meaningless, and
# the checks below compare it against a count of /Type /Page as well.
pages() {   # $1 = PDF; echoes a count, or nothing
  gs -q -dNOPAUSE -dBATCH -dSAFER -dNODISPLAY -dPDFINFO "$1" 2>/dev/null \
    | sed -n 's/^[[:space:]]*File has \([0-9][0-9]*\) pages*\.*$/\1/p' \
    | head -n 1
}

# The page objects, counted the naive way. /Pages is the tree node and must not
# be mistaken for a page, hence the trailing character class.
typepages() {   # $1 = PDF
  LC_ALL=C grep -aoE '/Type */Page[^s]' "$1" | wc -l | tr -d ' '
}

# Which pages came back, as the fingerprint left by pagesource above.
widths() {   # $1 = PDF; echoes e.g. "204 205 206"
  gs -q -dNOPAUSE -dBATCH -dSAFER -dNODISPLAY -dPDFINFO "$1" 2>/dev/null \
    | sed -n 's/^Page [0-9]* MediaBox: \[0 0 \([0-9]*\) .*/\1/p' \
    | tr '\n' ' '
}

# Run the converter over a document, with an optional header line in front of
# it. The exit status is one of the things under test, so it is captured rather
# than allowed to end the run.
run() {   # $1 = header fields, empty for no header; $2 = input file
  rc=0
  { [ -z "$1" ] || printf '%%%%ippfix %s\n' "$1"; cat "$2"; } \
    | ./defont > "$work/out" 2> "$work/err" || rc=$?
}

# The document, with any %%ippfix-out line taken off the front.
body() {
  if [ "$(head -c 12 "$work/out")" = '%%ippfix-out' ]; then
    tail -n +2 "$work/out" > "$work/body"
  else
    cp "$work/out" "$work/body"
  fi
}

echo 'page-range self-test'

echo 'the input page count is reported, and only when asked'
for count in 1 2 9; do
  run "report=1" "$work/p$count.pdf"
  check "report=1 counts a ${count} page document" \
        "[ \"\$(head -n 1 '$work/out')\" = '%%ippfix-out pages=$count' ]"
done
run "report=1" "$work/p9.pdf"
body
check 'the reported count matches what Ghostscript says' \
      "[ \"\$(pages '$work/body')\" = 9 ]"
check 'the document itself follows the report line intact' \
      "head -c 5 '$work/body' | grep -qa '%PDF-'"
check 'exactly one report line is emitted' \
      "[ \"\$(grep -ac '^%%ippfix-out' '$work/out')\" = 1 ]"

# Opt-in, because an older proxy that does not know to strip the line would
# hand the printer a PDF with a stray line in front of it.
run "" "$work/p9.pdf"
check 'no report line without a header at all' \
      "! grep -qa '%%ippfix-out' '$work/out'"
run "maxpdf=60000000 dpi=600" "$work/p9.pdf"
check 'no report line for a header carrying only the old fields' \
      "! grep -qa '%%ippfix-out' '$work/out'"
run "first=4 last=6" "$work/p9.pdf"
check 'no report line for a range that did not ask for one' \
      "! grep -qa '%%ippfix-out' '$work/out'"
run "report=yes" "$work/p9.pdf"
check 'report=yes is not report=1' "! grep -qa '%%ippfix-out' '$work/out'"
check 'report=yes says so on stderr' "grep -q 'ignoring report=yes' '$work/err'"
run "report=2" "$work/p9.pdf"
check 'a report= value from the future degrades to no report' \
      "! grep -qa '%%ippfix-out' '$work/out'"
check 'a document is still produced for report=2' \
      "head -c 5 '$work/out' | grep -qa '%PDF-'"

# The count is of the INPUT. A caller planning a split needs to know how much
# document there is, not how much of it this particular call returned.
run "first=4 last=6 report=1" "$work/p9.pdf"
check 'the reported count is the whole input, not the range' \
      "[ \"\$(head -n 1 '$work/out')\" = '%%ippfix-out pages=9' ]"
body
check 'the range is still what comes back' "[ \"\$(pages '$work/body')\" = 3 ]"

echo 'the header never reaches the document'
run "first=2 last=3 report=1 maxpdf=60000000" "$work/p9.pdf"
body
check 'the converted document starts with %PDF-' \
      "head -c 5 '$work/body' | grep -qa '%PDF-'"
check 'no %%ippfix line survives into the document' \
      "! grep -qa '%%ippfix ' '$work/body'"
run "report=1" "$work/raster.bin"
body
check 'a passed-through non-PDF is byte for byte the input' \
      "cmp -s '$work/body' '$work/raster.bin'"

echo 'a range returns exactly the pages asked for'
run "first=4 last=6" "$work/p9.pdf"
check 'pages 4-6 convert' "[ \"\$rc\" = 0 ]"
check 'pages 4-6 is three page objects' "[ \"\$(typepages '$work/out')\" = 3 ]"
check 'both ways of counting pages 4-6 agree' \
      "[ \"\$(pages '$work/out')\" = \"\$(typepages '$work/out')\" ]"
check 'pages 4-6 are pages 4, 5 and 6' \
      "[ \"\$(widths '$work/out')\" = '204 205 206 ' ]"

# Inclusive at both ends, which is where an off-by-one would show up: 4..6 is
# three pages and 4..4 is one, never two and never zero.
run "first=5 last=5" "$work/p9.pdf"
check 'first=last is a single page' "[ \"\$(typepages '$work/out')\" = 1 ]"
check 'first=last is that page and no other' \
      "[ \"\$(widths '$work/out')\" = '205 ' ]"
run "first=1 last=1" "$work/p1.pdf"
check 'the only page of a one page document' \
      "[ \"\$(typepages '$work/out')\" = 1 ]"
run "first=1 last=9" "$work/p9.pdf"
check 'first=1 last=total is the whole document' \
      "[ \"\$(typepages '$work/out')\" = 9 ]"
check 'the whole document comes back in order' \
      "[ \"\$(widths '$work/out')\" = '201 202 203 204 205 206 207 208 209 ' ]"
run "first=1 last=2" "$work/p2.pdf"
check 'a two page document taken whole' \
      "[ \"\$(typepages '$work/out')\" = 2 ]"

# One endpoint on its own is the obvious shorthand and Ghostscript supports it,
# so the converter must not read a missing endpoint as a missing range.
run "first=7" "$work/p9.pdf"
check 'first alone runs to the end' "[ \"\$(widths '$work/out')\" = '207 208 209 ' ]"
run "last=2" "$work/p9.pdf"
check 'last alone starts at page one' "[ \"\$(widths '$work/out')\" = '201 202 ' ]"

echo 'ranges tile a document without gap or overlap'
: > "$work/tiled"
total=0
for r in '1 3' '4 6' '7 9'; do
  set -- $r
  run "first=$1 last=$2" "$work/p9.pdf"
  check "pages $1-$2 come back as three pages" \
        "[ \"\$(typepages '$work/out')\" = 3 ]"
  widths "$work/out" >> "$work/tiled"
  total=$((total + $(typepages "$work/out")))
done
check 'three chunks of three make nine pages' "[ '$total' = 9 ]"
check 'every page appears exactly once, in order, across the chunks' \
      "[ \"\$(tr -d '\n' < '$work/tiled')\" = '201 202 203 204 205 206 207 208 209 ' ]"

echo 'a range that cannot mean what it says is refused'
# Nothing is written on any of these. Silently converting the whole document
# instead would be the worst possible answer: the caller is going to print what
# comes back next to the other chunks.
run "first=6 last=3" "$work/p9.pdf"
check 'a reversed range is refused' "[ \"\$rc\" = 2 ]"
check 'a reversed range writes nothing' "[ ! -s '$work/out' ]"
check 'a reversed range says why' "grep -q 'reversed range' '$work/err'"
for value in 0 -1 abc 1e2 0x2 '' ' '; do
  run "first=$value last=9" "$work/p9.pdf"
  check "first=${value:-<empty>} is refused" "[ \"\$rc\" = 2 ]"
  check "first=${value:-<empty>} writes nothing" "[ ! -s '$work/out' ]"
done
run "last=0" "$work/p9.pdf"
check 'last=0 is refused' "[ \"\$rc\" = 2 ]"
run "first=20 last=25" "$work/p9.pdf"
check 'a range wholly past the end is refused' "[ \"\$rc\" = 3 ]"
check 'a range past the end writes nothing, not a blank page' \
      "[ ! -s '$work/out' ]"
run "first=1 last=25" "$work/p9.pdf"
check 'a range overshooting the end stops at the last page' "[ \"\$rc\" = 0 ]"
check 'the overshoot is clamped to the pages that exist' \
      "[ \"\$(typepages '$work/out')\" = 9 ]"
check 'the clamp is announced' "grep -q 'past the end' '$work/err'"
run "first=1 last=2" "$work/raster.bin"
check 'a range of something that is not a PDF is refused' "[ \"\$rc\" = 3 ]"
check 'a non-PDF range writes nothing, not the file itself' \
      "[ ! -s '$work/out' ]"
run "report=1" "$work/raster.bin"
check 'a non-PDF reports zero pages rather than refusing' \
      "[ \"\$(head -n 1 '$work/out')\" = '%%ippfix-out pages=0' ]"

echo 'a fail-safe never substitutes the whole document for a range'
# Ghostscript here is stood in for, because the failures being tested are ones
# a working Ghostscript will not produce on demand. The stand-in answers
# -dPDFINFO from the real one, so the page count -- which the converter needs
# before it can decide anything -- is still the truth.
cat > "$work/gs-empty" <<'STUB'
#!/bin/bash
# Converts nothing: hands back an empty file, as a Ghostscript that died would.
for a in "$@"; do
  case "$a" in
    -dPDFINFO)      exec gs "$@" ;;
    -sOutputFile=*) out="${a#-sOutputFile=}" ;;
  esac
done
: > "$out"
STUB
cat > "$work/gs-fontfile" <<'STUB'
#!/bin/bash
# Converts normally but leaves a font program behind, which is the one thing
# the whole tool exists to remove.
for a in "$@"; do
  case "$a" in
    -dPDFINFO)      exec gs "$@" ;;
    -sOutputFile=*) out="${a#-sOutputFile=}" ;;
  esac
done
gs "$@" || exit $?
printf '%%/FontFile\n' >> "$out"
STUB
chmod +x "$work/gs-empty" "$work/gs-fontfile"

stubrun() {   # $1 = stub, $2 = header fields, $3 = input
  rc=0
  { [ -z "$2" ] || printf '%%%%ippfix %s\n' "$2"; cat "$3"; } \
    | GS="$work/$1" ./defont > "$work/out" 2> "$work/err" || rc=$?
}

# Without a range each of these still passes the input through, which is what
# it has always done and what the printer needs; with a range each writes
# nothing at all.
stubrun gs-empty "" "$work/p9.pdf"
check 'empty output still passes the whole input through' \
      "cmp -s '$work/out' '$work/p9.pdf'"
stubrun gs-empty "first=4 last=6" "$work/p9.pdf"
check 'empty output fails a range instead' "[ \"\$rc\" = 3 ]"
check 'empty output writes nothing for a range' "[ ! -s '$work/out' ]"
check 'empty output says it refused to substitute' \
      "grep -q 'refusing to send the whole document' '$work/err'"

stubrun gs-fontfile "" "$work/p9.pdf"
check 'a surviving font program still passes the input through' \
      "cmp -s '$work/out' '$work/p9.pdf'"
stubrun gs-fontfile "first=4 last=6" "$work/p9.pdf"
check 'a surviving font program fails a range instead' "[ \"\$rc\" = 3 ]"
check 'a surviving font program writes nothing for a range' \
      "[ ! -s '$work/out' ]"

# The lost-construct check compares the whole input against a part of it, so a
# gradient that lives only on page one reads as lost when pages 4-6 are asked
# for. That false alarm is deliberate -- see the comment on the check itself --
# and what matters here is which way it fails.
run "" "$work/shade.pdf"
check 'a document with a gradient converts whole' \
      "[ \"\$(typepages '$work/out')\" = 9 ]"
run "first=4 last=6" "$work/shade.pdf"
check 'a construct missing from a range fails the range' "[ \"\$rc\" = 3 ]"
check 'a construct missing from a range writes nothing' "[ ! -s '$work/out' ]"
check 'it says the construct may simply be outside the range' \
      "grep -q 'outside 4-6' '$work/err'"

echo 'a range that is still too large is handed back, not quietly rasterised'
# The caller picked the chunk size and can pick a smaller one. Rasterising one
# chunk in the middle of a document would change how it looks next to its
# neighbours without the caller ever finding out.
run "first=4 last=6 maxpdf=1000" "$work/p9.pdf"
check 'an oversized range is refused' "[ \"\$rc\" = 4 ]"
check 'an oversized range writes nothing' "[ ! -s '$work/out' ]"
check 'an oversized range asks for a smaller one' \
      "grep -q 'smaller range' '$work/err'"
# A single page is the end of that argument: there is nothing left to cut, so
# raster is the only thing left that prints at all.
run "first=5 last=5 maxpdf=1000" "$work/p9.pdf"
check 'a single oversized page rasterises' "[ \"\$rc\" = 0 ]"
check 'the single page comes back as raster' \
      "head -c 7 '$work/out' | grep -qa UNIRAST"
check 'the tier change is said out loud' \
      "grep -q 'not look quite like its neighbours' '$work/err'"
# Rasterising the whole document in answer to a request for one page of it
# would be the same mistake as passing the whole input through.
run "maxpdf=1000" "$work/p9.pdf"
cp "$work/out" "$work/whole.raster"
run "first=5 last=5 maxpdf=1000" "$work/p9.pdf"
check 'only the requested page is rasterised' \
      "[ \"\$(stat -c%s '$work/out')\" -lt \"\$(( \$(stat -c%s '$work/whole.raster') / 4 ))\" ]"
run "first=5 last=5 maxpdf=1000 raster=none" "$work/p9.pdf"
check 'with no raster format the single page is sent as PDF anyway' \
      "head -c 5 '$work/out' | grep -qa '%PDF-'"
check 'and it is still only the one page' "[ \"\$(typepages '$work/out')\" = 1 ]"

echo 'nothing changes for a caller that never mentions a range'
run "" "$work/p9.pdf"
check 'a headerless document converts as before' "[ \"\$rc\" = 0 ]"
check 'and comes back whole' "[ \"\$(typepages '$work/out')\" = 9 ]"
check 'and has no font programs left' "! grep -qa '/FontFile' '$work/out'"
run "device=appleraster colorspace=19 dpi=600 maxpdf=60000000" "$work/p9.pdf"
check 'an old-style header converts as before' \
      "[ \"\$(typepages '$work/out')\" = 9 ]"
check 'an old-style header is not reported as unknown' \
      "! grep -q 'unknown header field' '$work/err'"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
