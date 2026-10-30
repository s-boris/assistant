/* netguard_eval.c: LD_PRELOAD network guard for eval workers (`evals`).
 * Blocks connect/sendto/sendmsg/sendmmsg to loopback (except Docker DNS 127.0.0.11:53), private/LAN,
 * link-local, CGNAT, multicast/reserved and the Telegram ranges, for IPv4, IPv6 and v4-mapped IPv6.
 * The public internet stays reachable (the model API). Blocked attempts are appended to $NETGUARD_LOG.
 * Build: gcc -shared -fPIC -O2 -o netguard-eval.so netguard_eval.c -ldl   (evals builds it on demand). */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

struct net4 { uint32_t net, mask; };
static const struct net4 BLOCK4[] = {
  {0x00000000u, 0xff000000u}, {0x7f000000u, 0xff000000u}, {0x0a000000u, 0xff000000u},
  {0xac100000u, 0xfff00000u}, {0xc0a80000u, 0xffff0000u}, {0xa9fe0000u, 0xffff0000u},
  {0x64400000u, 0xffc00000u}, {0xe0000000u, 0xf0000000u}, {0xf0000000u, 0xf0000000u},
  /* Telegram */
  {0x959aa000u, 0xfffff000u}, {0x5b6c0400u, 0xfffffc00u}, {0x5b6c0800u, 0xfffffc00u},
  {0x5b6c0c00u, 0xfffffc00u}, {0x5b6c1000u, 0xfffffc00u}, {0x5b6c1400u, 0xfffffc00u},
  {0x5b6c3800u, 0xfffffc00u}, {0x5fa14000u, 0xfffff000u}, {0xb94c9700u, 0xffffff00u},
};

static int v4_blocked(uint32_t ip, int port, char *why, size_t n) {
  if (ip == 0x7f00000bu && port == 53) return 0;            /* Docker DNS */
  for (size_t i = 0; i < sizeof BLOCK4 / sizeof *BLOCK4; i++)
    if ((ip & BLOCK4[i].mask) == BLOCK4[i].net) {
      struct in_addr a = { htonl(ip) }; char b[32]; inet_ntop(AF_INET, &a, b, sizeof b);
      snprintf(why, n, "%s:%d", b, port); return 1;
    }
  return 0;
}

static int v6_blocked(const struct in6_addr *a, int port, char *why, size_t n) {
  const uint8_t *s = a->s6_addr;
  static const uint8_t mapped[12] = {0,0,0,0,0,0,0,0,0,0,0xff,0xff};
  if (!memcmp(s, mapped, 12)) {
    uint32_t ip = ((uint32_t)s[12] << 24) | ((uint32_t)s[13] << 16) | ((uint32_t)s[14] << 8) | s[15];
    return v4_blocked(ip, port, why, n);
  }
  int zero = 1; for (int i = 0; i < 15; i++) if (s[i]) { zero = 0; break; }
  int bad = (zero && (s[15] == 1 || s[15] == 0))            /* ::1, :: */
         || (s[0] & 0xfe) == 0xfc                          /* fc00::/7 */
         || (s[0] == 0xfe && (s[1] & 0xc0) == 0x80)        /* fe80::/10 */
         || s[0] == 0xff                                    /* multicast */
         || (s[0] == 0x20 && s[1] == 0x01 && s[2] == 0x0b && s[3] == 0x28)               /* Telegram */
         || (s[0] == 0x20 && s[1] == 0x01 && s[2] == 0x06 && s[3] == 0x7c && s[4] == 0x04 && s[5] == 0xe8)
         || (s[0] == 0x2a && s[1] == 0x0a && s[2] == 0xf2 && s[3] == 0x80);
  if (bad) { char b[64]; inet_ntop(AF_INET6, a, b, sizeof b); snprintf(why, n, "[%s]:%d", b, port); }
  return bad;
}

static int blocked(const struct sockaddr *a, socklen_t len, char *why, size_t n) {
  if (!a || len < (socklen_t)sizeof(sa_family_t)) return 0;
  if (a->sa_family == AF_INET && len >= (socklen_t)sizeof(struct sockaddr_in)) {
    const struct sockaddr_in *s = (const void *)a;
    return v4_blocked(ntohl(s->sin_addr.s_addr), ntohs(s->sin_port), why, n);
  }
  if (a->sa_family == AF_INET6 && len >= (socklen_t)sizeof(struct sockaddr_in6)) {
    const struct sockaddr_in6 *s = (const void *)a;
    return v6_blocked(&s->sin6_addr, ntohs(s->sin6_port), why, n);
  }
  return 0;
}

static void logit(const char *op, const char *why) {
  const char *f = getenv("NETGUARD_LOG"); if (!f) return;
  int fd = open(f, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0644); if (fd < 0) return;
  char comm[64] = "?"; int cf = open("/proc/self/comm", O_RDONLY | O_CLOEXEC);
  if (cf >= 0) { ssize_t r = read(cf, comm, sizeof comm - 1); if (r > 0) comm[r - (comm[r-1] == '\n')] = 0; close(cf); }
  char line[256]; int k = snprintf(line, sizeof line, "%ld %d %s %s %s\n", (long)time(NULL), (int)getpid(), comm, op, why);
  if (k > 0) { ssize_t w = write(fd, line, (size_t)k); (void)w; }
  close(fd);
}

typedef int (*connect_fn)(int, const struct sockaddr *, socklen_t);
typedef ssize_t (*sendto_fn)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
typedef ssize_t (*sendmsg_fn)(int, const struct msghdr *, int);
typedef int (*sendmmsg_fn)(int, struct mmsghdr *, unsigned int, int);
#define REAL(name) static name##_fn real_##name; if (!real_##name) real_##name = (name##_fn)dlsym(RTLD_NEXT, #name)

int connect(int fd, const struct sockaddr *a, socklen_t len) {
  REAL(connect);
  char why[96];
  if (blocked(a, len, why, sizeof why)) { logit("connect", why); errno = ENETUNREACH; return -1; }
  return real_connect(fd, a, len);
}

ssize_t sendto(int fd, const void *b, size_t n, int fl, const struct sockaddr *a, socklen_t len) {
  REAL(sendto);
  char why[96];
  if (blocked(a, len, why, sizeof why)) { logit("sendto", why); errno = ENETUNREACH; return -1; }
  return real_sendto(fd, b, n, fl, a, len);
}

ssize_t sendmsg(int fd, const struct msghdr *m, int fl) {
  REAL(sendmsg);
  char why[96];
  if (m && blocked(m->msg_name, m->msg_namelen, why, sizeof why)) { logit("sendmsg", why); errno = ENETUNREACH; return -1; }
  return real_sendmsg(fd, m, fl);
}

int sendmmsg(int fd, struct mmsghdr *v, unsigned int n, int fl) {
  REAL(sendmmsg);
  char why[96];
  for (unsigned int i = 0; v && i < n; i++)
    if (blocked(v[i].msg_hdr.msg_name, v[i].msg_hdr.msg_namelen, why, sizeof why)) { logit("sendmmsg", why); errno = ENETUNREACH; return -1; }
  return real_sendmmsg(fd, v, n, fl);
}
