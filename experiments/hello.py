"""A simple hello module demonstrating basic Python functions."""


def greet(name: str) -> str:
    """Generate a greeting message for the given name.

    Args:
        name: The name of the person to greet.

    Returns:
        A formatted greeting string.
    """
    return f"Hello, {name}!"


def add(a: int | float, b: int | float) -> int | float:
    """Add two numbers together.

    Args:
        a: The first number.
        b: The second number.

    Returns:
        The sum of a and b.
    """
    return a + b


def main() -> None:
    """Run the main program logic."""
    print(greet("World"))
    print(f"2 + 3 = {add(2, 3)}")


if __name__ == "__main__":
    main()
