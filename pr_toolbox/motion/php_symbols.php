<?php

// SPDX-License-Identifier: GPL-3.0-or-later

/**
 * The symbols in PHP files, by PHP's own tokenizer: for each file, its namespace, its classes, interfaces,
 * traits and enums (with what they extend and implement, and whether abstract), each one's methods (name,
 * first and last line, abstract or not, and the signature as written), and its global functions.
 *
 *   php php_symbols.php <folder of files>  > symbols.json
 *
 * Reads the files; runs nothing from them. A file that doesn't tokenize is listed with an error.
 */

declare(strict_types=1);

$dir = $argv[1] ?? '';
if (!is_dir($dir)) {
    fwrite(STDERR, "usage: php php_symbols.php <folder>\n");
    exit(2);
}

/** @return array<string, mixed> */
function symbols(string $code): array
{
    $tokens = token_get_all($code);
    $n = count($tokens);
    $namespace = '';
    $classes = [];
    $functions = [];
    $depth = 0;
    $classStack = []; // [name, depthAtOpen]
    $pendingClass = null;
    $i = 0;
    $text = static fn($t): string => is_array($t) ? $t[1] : $t;
    $line = static function (int $i) use ($tokens, $n): int {
        for ($j = $i; $j < $n; $j++) {
            if (is_array($tokens[$j])) {
                return $tokens[$j][2];
            }
        }
        return 0;
    };
    while ($i < $n) {
        $t = $tokens[$i];
        $id = is_array($t) ? $t[0] : null;
        if ($id === T_NAMESPACE) {
            $ns = '';
            for ($j = $i + 1; $j < $n && $tokens[$j] !== ';' && $tokens[$j] !== '{'; $j++) {
                $ns .= trim($text($tokens[$j]));
            }
            $namespace = $ns;
        } elseif (in_array($id, [T_CLASS, T_INTERFACE, T_TRAIT, defined('T_ENUM') ? T_ENUM : -1], true)) {
            // skip "::class" and anonymous classes
            $prev = $i - 1;
            while ($prev >= 0 && is_array($tokens[$prev]) && $tokens[$prev][0] === T_WHITESPACE) {
                $prev--;
            }
            $isConst = $prev >= 0 && is_array($tokens[$prev]) && $tokens[$prev][0] === T_DOUBLE_COLON;
            $j = $i + 1;
            while ($j < $n && is_array($tokens[$j]) && $tokens[$j][0] === T_WHITESPACE) {
                $j++;
            }
            if (!$isConst && $j < $n && is_array($tokens[$j]) && $tokens[$j][0] === T_STRING) {
                $abstract = false;
                for ($k = $prev; $k >= 0 && $k > $prev - 6; $k--) {
                    if (is_array($tokens[$k]) && $tokens[$k][0] === T_ABSTRACT) {
                        $abstract = true;
                    }
                }
                $header = '';
                for ($k = $j + 1; $k < $n && $tokens[$k] !== '{'; $k++) {
                    $header .= $text($tokens[$k]);
                }
                $extends = preg_match('/\bextends\s+([\\\\\w\s,]+?)(?:\bimplements\b|$)/', $header, $m) ? array_map('trim', explode(',', $m[1])) : [];
                $implements = preg_match('/\bimplements\s+([\\\\\w\s,]+)$/', trim($header), $m) ? array_map('trim', explode(',', $m[1])) : [];
                $kind = token_name($id);
                $pendingClass = ['name' => $tokens[$j][1], 'kind' => strtolower(substr($kind, 2)), 'abstract' => $abstract,
                    'extends' => array_values(array_filter($extends)), 'implements' => array_values(array_filter($implements)),
                    'start' => $tokens[$j][2], 'end' => 0, 'methods' => []];
            }
        } elseif ($id === T_FUNCTION) {
            $j = $i + 1;
            while ($j < $n && is_array($tokens[$j]) && $tokens[$j][0] === T_WHITESPACE) {
                $j++;
            }
            if ($j < $n && $tokens[$j] === '&') {
                $j++;
                while ($j < $n && is_array($tokens[$j]) && $tokens[$j][0] === T_WHITESPACE) {
                    $j++;
                }
            }
            if ($j < $n && is_array($tokens[$j]) && $tokens[$j][0] === T_STRING) {
                $name = $tokens[$j][1];
                $start = $tokens[$j][2];
                $abstract = false;
                for ($k = $i - 1; $k >= 0 && $k > $i - 10; $k--) {
                    if (is_array($tokens[$k]) && $tokens[$k][0] === T_ABSTRACT) {
                        $abstract = true;
                    }
                    if (!is_array($tokens[$k]) && in_array($tokens[$k], [';', '{', '}'], true)) {
                        break;
                    }
                }
                // signature: from "(" to "{" or ";"
                $sig = '';
                $k = $j + 1;
                $paren = 0;
                for (; $k < $n; $k++) {
                    $tx = $text($tokens[$k]);
                    if ($tokens[$k] === '(') {
                        $paren++;
                    } elseif ($tokens[$k] === ')') {
                        $paren--;
                    }
                    if ($paren === 0 && ($tokens[$k] === '{' || $tokens[$k] === ';')) {
                        break;
                    }
                    $sig .= $tx;
                }
                $sig = preg_replace('/\s+/', ' ', trim($sig));
                $end = $start;
                if ($k < $n && $tokens[$k] === '{') {
                    $d = 0;
                    for ($m2 = $k; $m2 < $n; $m2++) {
                        if ($tokens[$m2] === '{' || (is_array($tokens[$m2]) && in_array($tokens[$m2][0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true))) {
                            $d++;
                        } elseif ($tokens[$m2] === '}') {
                            $d--;
                            if ($d === 0) {
                                $end = $line($m2) ?: $end;
                                break;
                            }
                        }
                    }
                } else {
                    $end = $line($k) ?: $start;
                }
                $entry = ['name' => $name, 'start' => $start, 'end' => $end, 'abstract' => $abstract || ($k < $n && $tokens[$k] === ';'), 'signature' => $sig];
                if ($classStack !== []) {
                    $classes[count($classes) - 1]['methods'][] = $entry;
                } else {
                    $functions[] = $entry;
                }
            }
        }
        if ($t === '{' || (is_array($t) && in_array($t[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true))) {
            $depth++;
            if ($pendingClass !== null && $t === '{') {
                $classes[] = $pendingClass;
                $classStack[] = $depth;
                $pendingClass = null;
            }
        } elseif ($t === '}') {
            if ($classStack !== [] && end($classStack) === $depth) {
                array_pop($classStack);
                $classes[count($classes) - 1]['end'] = is_array($tokens[$i]) ? $tokens[$i][2] : $line($i);
            }
            $depth--;
        }
        $i++;
    }
    return ['namespace' => $namespace, 'classes' => $classes, 'functions' => $functions];
}

$out = [];
foreach (new DirectoryIterator($dir) as $f) {
    if ($f->isDot() || !$f->isFile()) {
        continue;
    }
    $code = (string)file_get_contents($f->getPathname());
    try {
        $out[$f->getFilename()] = symbols($code);
    } catch (\Throwable $e) {
        $out[$f->getFilename()] = ['error' => $e->getMessage()];
    }
}
echo json_encode($out);
