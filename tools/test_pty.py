import sys
import pexpect
print("Starting...")
child = pexpect.spawn("bash", encoding='utf-8')
child.expect(pexpect.EOF)
print(child.before)
