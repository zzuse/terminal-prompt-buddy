import sys, tty, termios
import asyncio

async def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    queue = asyncio.Queue()
    
    def on_stdin():
        ch = sys.stdin.read(1)
        if ch:
            queue.put_nowait(ch)
            
    try:
        tty.setraw(fd)
        loop = asyncio.get_event_loop()
        loop.add_reader(fd, on_stdin)
        
        while True:
            ch = await queue.get()
            sys.stdout.write(f"\\x{ord(ch):02x}\r\n")
            sys.stdout.flush()
            if ch == 'q':
                break
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

asyncio.run(main())
