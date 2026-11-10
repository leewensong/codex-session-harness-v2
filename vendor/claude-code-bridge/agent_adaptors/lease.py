"""OS-held bridge-state lease; crash releases it, never delete provider locks."""
import os
from .contracts import AdaptorError

class StateLease:
    def __init__(self, path):
        self.file = path.open('a+b')
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise AdaptorError('state_in_use', 'bridge state is already owned by another instance') from None

    def close(self):
        if self.file.closed: return
        self.file.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
