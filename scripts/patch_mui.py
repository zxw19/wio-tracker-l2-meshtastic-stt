import io, sys
p = r"C:\Users\seeed\Documents\ChatGPT\L2_Demo\research\meshtastic-firmware\.pio\libdeps\seeed_wio_tracker_L2-tft\meshtastic-device-ui\source\graphics\TFT\TFTView_320x240.cpp"
s = io.open(p, "r", encoding="utf-8", newline="").read()

def rep(s, old, new, tag):
    if s.count(old) != 1:
        print("ANCHOR FAIL:", tag, s.count(old)); sys.exit(1)
    return s.replace(old, new)

old1 = """    if (to == UINT32_MAX) { // message for group, prepend short name to msg
        const char *userShort = nodeShortName(from);
        if (!userShort) {
            pos += sprintf(buf, "%04x ", from & 0xffff);
        } else {
            while (pos < 4 && userShort[pos] != 0) {
                buf[pos] = userShort[pos];
                pos++;
            }
        }
        buf[pos++] = ' ';
        container = channelGroup[ch];
    } else { // message for us"""
new1 = """    if (to == UINT32_MAX) { // message for group, prepend short name to msg
        // Messages that came from this device (e.g. voice input) are ours: keep the bubble clean and
        // let it render right aligned instead of looking like a stranger's message.
        if (from != ownNode) {
            const char *userShort = nodeShortName(from);
            if (!userShort) {
                pos += sprintf(buf, "%04x ", from & 0xffff);
            } else {
                while (pos < 4 && userShort[pos] != 0) {
                    buf[pos] = userShort[pos];
                    pos++;
                }
            }
            buf[pos++] = ' ';
        }
        container = channelGroup[ch];
    } else { // message for us"""
s = rep(s, old1, new1, "prefix")

old2 = """void TFTView_320x240::newMessage(uint32_t nodeNum, lv_obj_t *container, uint8_t ch, const char *msg)
{
    ChatHistory::Entry entry;
    entry.text = msg;
    entry.node = nodeNum;
    appendChatEntry(container, std::move(entry));
}"""
new2 = """void TFTView_320x240::newMessage(uint32_t nodeNum, lv_obj_t *container, uint8_t ch, const char *msg)
{
    ChatHistory::Entry entry;
    entry.text = msg;
    entry.node = nodeNum;
    // Anything whose sender is this node is ours, however it was produced (keyboard or voice).
    entry.sent = (nodeNum == ownNode);
    appendChatEntry(container, std::move(entry));
}"""
s = rep(s, old2, new2, "sent flag")

io.open(p, "w", encoding="utf-8", newline="").write(s)
print("mui patched")
