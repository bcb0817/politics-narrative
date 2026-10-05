"""Manual article entry point. Deleted daemon/post commands are not restored."""
import sys
from src.article_generation import main

if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='short':
        from src.short_posts import main as short_main
        sys.exit(short_main(sys.argv[2:]))
    if len(sys.argv)>1 and sys.argv[1]=='rss':
        from src.rss_candidates import main as rss_main
        sys.exit(rss_main(sys.argv[2:]))
    if len(sys.argv)<2 or sys.argv[1]!='article':
        print('Usage: python local_bot.py article --help | rss --help (no daemon or posting commands)')
        sys.exit(2)
    sys.exit(main(sys.argv[2:]))
