# Rebuilding `app/ui/vendor/highlight.min.js`

The vendored highlight.js is a **custom build**, not a stock release. It contains
`highlight.js/lib/core` plus exactly the grammars that Docling's
`CodeLanguageLabel` enum can actually name — nothing else. The stock "common"
build carries languages Docling never emits, and the full build is several times
larger, so neither is used.

There is no Node dependency in the app itself; this is a one-off build step run
by hand whenever highlight.js is upgraded or the language set changes.

## Rebuild

```bash
mkdir hljs-build && cd hljs-build
npm init -y
npm install highlight.js esbuild
```

Write `entry.js` importing core plus each grammar listed below, registering each
one under its own name, then:

```js
hljs.configure({ ignoreUnescapedHTML: true, throwUnescapedHTML: false });
window.hljs = hljs;
```

`ignoreUnescapedHTML` matters: the app highlights nodes that are already in the
DOM and already sanitised by DOMPurify, which is exactly the pattern highlight.js
warns about by default. The warning is noise here, not a signal.

Then bundle and prepend the provenance header that is currently at the top of
`highlight.min.js`:

```bash
npx esbuild entry.js --bundle --minify --format=iife --target=es2019 \
  --legal-comments=none --outfile=hljs.bundle.js
```

## Language set (48 grammars)

```
ada awk bash c clojure cmake cpp crystal csharp css d dart delphi dockerfile
elixir erlang fortran go haskell haxe java javascript json julia kotlin latex
lisp lua matlab moonscript nim objectivec ocaml perl php prolog python ruby
rust scala scheme sml sql swift typescript vbnet xml yaml
```

## Label mapping

54 of Docling's 61 `CodeLanguageLabel` values map onto these 48 grammars. Several
labels share one grammar, and the names are not always the obvious lowercase:

| Docling label | highlight.js grammar |
|---|---|
| `C#` | `csharp` |
| `C++`, `Cuda` | `cpp` |
| `FORTRAN` | `fortran` |
| `ObjectiveC` | `objectivec` |
| `VisualBasic` | `vbnet` |
| `HTML`, `XML` | `xml` |
| `Latex`, `Tikz` | `latex` |
| `Octave` | `matlab` |
| `Cython` | `python` |
| `Racket` | `scheme` |
| `Pascal` | `delphi` |

The remaining seven produce a plain, unlabelled code block: `unknown` and
`DocLang` by definition, and `bc`, `dc`, `Ceylon`, `Forth` and `COBOL` because
highlight.js ships no grammar for them (COBOL exists only as a third-party
plugin).

This table is the client-side half of the mapping. The authoritative conversion
from a `CodeLanguageLabel` to a Markdown fence identifier happens in Python, so
that the saved `.md` is correct for every reader and not just this app's
preview.
