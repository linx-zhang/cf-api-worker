count = 0


def get_hello_message():
    global count
    count += 1
    return str(count)
