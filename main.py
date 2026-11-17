from core.router import chat


def main():
    print("=" * 40)
    print("        AMIKI v0.1.0")
    print("   Personal AI Assistant")
    print("=" * 40)
    print()
    print("Type 'exit' or 'quit' to stop.")
    print()

    while True:
        try:
            user_input = input("You: ").strip()

            if not user_input:
                continue

            if user_input.lower() in {"exit", "quit"}:
                print("Amiki: Goodbye.")
                break

            try:
                response = chat(user_input)
            except Exception as error:
                print(f"Amiki: ERROR: Unexpected failure: {error}")
                continue

            print()
            print("Amiki:", response)
            print()

        except KeyboardInterrupt:
            print("\nAmiki: Goodbye.")
            break

        except EOFError:
            print("\nAmiki: Goodbye.")
            break


if __name__ == "__main__":
    main()
