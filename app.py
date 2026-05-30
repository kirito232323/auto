import argparse
import re
import sys

EXPR_PATTERN = re.compile(r"^\s*(?P<expr>.+?)\s*(?:=\s*(?P<given>.+))?$")
ALLOWED_NAMES = {"abs": abs, "round": round, "min": min, "max": max, "pow": pow}


def parse_input(text: str):
    match = EXPR_PATTERN.match(text)
    if not match:
        raise ValueError("Provide an expression, optionally followed by '= answer'.")
    expr = match.group("expr").strip()
    given = match.group("given")
    return expr, given.strip() if given else None


def safe_eval(expr: str):
    code = compile(expr, "<math>", "eval")
    for name in code.co_names:
        if name not in ALLOWED_NAMES:
            raise ValueError(f"Unsafe expression element: {name}")
    return eval(code, {"__builtins__": {}}, ALLOWED_NAMES)


def main():
    parser = argparse.ArgumentParser(description="Solve a math expression and optionally verify a provided result.")
    parser.add_argument("problem", nargs="*", help="Math expression, optionally with '= answer'.")
    args = parser.parse_args()

    text = " ".join(args.problem) if args.problem else input("Problem: ")

    try:
        expr, given = parse_input(text)
        answer = safe_eval(expr)
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"Expression: {expr}")
    print(f"Answer: {answer}")

    if given is None:
        return

    print(f"Provided answer: {given}")
    try:
        given_value = safe_eval(given)
    except Exception:
        print(f"Verification: could not parse '{given}'.")
        return

    if given_value == answer:
        print("Verification: correct")
    else:
        print(f"Verification: incorrect (parsed = {given_value})")


if __name__ == "__main__":
    main()
