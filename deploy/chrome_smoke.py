from DrissionPage import Chromium, ChromiumOptions


options = ChromiumOptions().auto_port()
options.set_browser_path("google-chrome")
options.set_argument("--no-proxy-server")
browser = Chromium(options)
try:
    print("chromium-smoke=OK")
finally:
    browser.quit()
