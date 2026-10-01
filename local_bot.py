"""Manual article entry point. Deleted daemon/post commands are not restored."""
import sys
from src.article_generation import main

if __name__ == '__main__':
    if len(sys.argv)<2 or sys.argv[1]!='article':
        print('Usage: python local_bot.py article --help (no daemon or posting commands)')
        sys.exit(2)
    sys.exit(main(sys.argv[2:]))
