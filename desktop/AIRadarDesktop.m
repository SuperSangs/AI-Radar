#import <Cocoa/Cocoa.h>
#import <ServiceManagement/ServiceManagement.h>
#import <WebKit/WebKit.h>
#import <arpa/inet.h>
#import <sys/socket.h>
#import <unistd.h>

static NSString * const kAPIBase = @"http://127.0.0.1:8765";

static BOOL AIRadarLocalPortIsOpen(void) {
    int socketFD = socket(AF_INET, SOCK_STREAM, 0);
    if (socketFD < 0) return NO;
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons(8765);
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
    BOOL connected = connect(socketFD, (struct sockaddr *)&address, sizeof(address)) == 0;
    close(socketFD);
    return connected;
}

@interface BallView : NSView
@property(nonatomic, copy) dispatch_block_t clickHandler;
@property(nonatomic, copy) dispatch_block_t quitHandler;
@property(nonatomic) BOOL active;
@property(nonatomic) BOOL dragging;
@property(nonatomic) NSPoint dragStart;
@property(nonatomic) NSPoint windowStart;
@end

@implementation BallView
- (void)drawRect:(NSRect)dirtyRect {
    NSRect circle = NSInsetRect(self.bounds, 2, 2);
    [[NSColor colorWithCalibratedRed:0.08 green:0.14 blue:0.13 alpha:0.98] setFill];
    [[NSBezierPath bezierPathWithOvalInRect:circle] fill];
    [[NSColor colorWithCalibratedRed:0.78 green:0.96 blue:0.42 alpha:1] setStroke];
    NSBezierPath *ring = [NSBezierPath bezierPathWithOvalInRect:NSInsetRect(circle, 1, 1)];
    ring.lineWidth = self.active ? 3 : 2;
    [ring stroke];
    NSDictionary *attrs = @{
        NSFontAttributeName: [NSFont systemFontOfSize:13 weight:NSFontWeightBold],
        NSForegroundColorAttributeName: [NSColor colorWithCalibratedRed:0.78 green:0.96 blue:0.42 alpha:1]
    };
    NSAttributedString *label = [[NSAttributedString alloc] initWithString:@"AI" attributes:attrs];
    NSSize size = label.size;
    [label drawAtPoint:NSMakePoint(NSMidX(circle) - size.width / 2, NSMidY(circle) - size.height / 2 + 1)];
}
- (void)mouseDown:(NSEvent *)event {
    self.dragStart = NSEvent.mouseLocation;
    self.windowStart = self.window.frame.origin;
    self.dragging = NO;
}
- (void)mouseDragged:(NSEvent *)event {
    NSPoint current = NSEvent.mouseLocation;
    CGFloat dx = current.x - self.dragStart.x;
    CGFloat dy = current.y - self.dragStart.y;
    if (!self.dragging && hypot(dx, dy) > 4) self.dragging = YES;
    if (self.dragging) [self.window setFrameOrigin:NSMakePoint(self.windowStart.x + dx, self.windowStart.y + dy)];
}
- (void)mouseUp:(NSEvent *)event {
    if (!self.dragging && self.clickHandler) self.clickHandler();
    self.dragging = NO;
}
- (void)rightMouseDown:(NSEvent *)event {
    NSMenu *menu = [[NSMenu alloc] initWithTitle:@"AI Radar"];
    NSMenuItem *quit = [[NSMenuItem alloc] initWithTitle:@"退出 AI Radar" action:@selector(quitRadar:) keyEquivalent:@""];
    quit.target = self;
    [menu addItem:quit];
    [NSMenu popUpContextMenu:menu withEvent:event forView:self];
}
- (void)quitRadar:(id)sender { if (self.quitHandler) self.quitHandler(); }
- (void)resetCursorRects { [self addCursorRect:self.bounds cursor:[NSCursor pointingHandCursor]]; }
@end

@interface AppDelegate : NSObject <NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate>
@property(nonatomic, strong) NSPanel *ballWindow;
@property(nonatomic, strong) NSPanel *panelWindow;
@property(nonatomic, strong) BallView *ballView;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSTextField *nativeStatus;
@property(nonatomic, strong) NSTimer *timer;
@property(nonatomic, strong) NSTask *tunnelTask;
@property(nonatomic, strong) NSMutableArray<NSString *> *pendingScripts;
@property(nonatomic) BOOL webViewReady;
@property(nonatomic) BOOL panelVisible;
@property(nonatomic) BOOL tunnelRetryScheduled;
@property(nonatomic) BOOL terminating;
@property(nonatomic) NSUInteger tunnelRetryCount;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    pid_t currentPID = NSProcessInfo.processInfo.processIdentifier;
    for (NSRunningApplication *application in [NSRunningApplication runningApplicationsWithBundleIdentifier:@"com.sangshuai.ai-radar-desktop"]) {
        if (application.processIdentifier == currentPID) continue;
        [application terminate];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            if (!application.terminated) [application forceTerminate];
        });
    }
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    [self registerLoginItem];
    self.pendingScripts = [NSMutableArray array];
    [self buildBall];
    [self buildPanel];
    [self ensureTunnel];
    [self reloadContent];
    self.timer = [NSTimer scheduledTimerWithTimeInterval:60 target:self selector:@selector(reloadContent) userInfo:nil repeats:YES];
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    self.terminating = YES;
    [self.timer invalidate];
    self.tunnelTask.terminationHandler = nil;
    if (self.tunnelTask.isRunning) [self.tunnelTask terminate];
}

- (void)registerLoginItem {
    if (@available(macOS 13.0, *)) {
        SMAppService *service = SMAppService.mainAppService;
        if (service.status == SMAppServiceStatusNotRegistered) {
            NSError *error = nil;
            if (![service registerAndReturnError:&error]) {
                NSLog(@"AI Radar could not register as a login item: %@", error);
            }
        } else if (service.status == SMAppServiceStatusRequiresApproval) {
            NSLog(@"AI Radar login item requires approval in System Settings");
        }
    }
}

- (void)ensureTunnel {
    if (self.terminating || AIRadarLocalPortIsOpen() || self.tunnelTask.isRunning) return;
    NSString *plistPath = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/LaunchAgents/com.sangshuai.ai-radar-tunnel.plist"];
    NSDictionary *configuration = [NSDictionary dictionaryWithContentsOfFile:plistPath];
    NSArray *arguments = configuration[@"ProgramArguments"];
    if (![arguments isKindOfClass:NSArray.class] || arguments.count < 2 || ![arguments[0] isEqualToString:@"/usr/bin/ssh"]) {
        NSLog(@"AI Radar tunnel configuration is missing or invalid: %@", plistPath);
        return;
    }

    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:arguments[0]];
    task.arguments = [arguments subarrayWithRange:NSMakeRange(1, arguments.count - 1)];
    task.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    task.standardError = [NSFileHandle fileHandleWithNullDevice];
    __weak typeof(self) weakSelf = self;
    task.terminationHandler = ^(NSTask *finishedTask) {
        dispatch_async(dispatch_get_main_queue(), ^{
            typeof(self) strongSelf = weakSelf;
            if (!strongSelf || strongSelf.terminating) return;
            if (strongSelf.tunnelTask == finishedTask) strongSelf.tunnelTask = nil;
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(5 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
                [strongSelf ensureTunnel];
            });
        });
    };
    self.tunnelTask = task;
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        self.tunnelTask = nil;
        NSLog(@"AI Radar could not start its SSH tunnel: %@", error);
    }
}

- (void)scheduleTunnelRetry {
    if (self.tunnelRetryScheduled || self.terminating) return;
    self.tunnelRetryScheduled = YES;
    [self ensureTunnel];
    NSUInteger exponent = MIN(self.tunnelRetryCount, (NSUInteger)3);
    NSTimeInterval delay = MIN(30.0, 3.0 * (1 << exponent));
    self.tunnelRetryCount += 1;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(delay * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        self.tunnelRetryScheduled = NO;
        [self loadFeed];
        [self loadDailyBrief];
    });
}

- (void)buildBall {
    CGFloat size = 54;
    NSRect screen = [NSScreen mainScreen].visibleFrame;
    NSRect frame = NSMakeRect(NSMaxX(screen) - size - 18, NSMidY(screen) - size / 2, size, size);
    self.ballWindow = [[NSPanel alloc] initWithContentRect:frame styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel backing:NSBackingStoreBuffered defer:NO];
    self.ballWindow.opaque = NO;
    self.ballWindow.backgroundColor = NSColor.clearColor;
    self.ballWindow.hasShadow = YES;
    self.ballWindow.level = NSFloatingWindowLevel;
    self.ballWindow.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
    self.ballWindow.hidesOnDeactivate = NO;
    self.ballView = [[BallView alloc] initWithFrame:NSMakeRect(0, 0, size, size)];
    __weak typeof(self) weakSelf = self;
    self.ballView.clickHandler = ^{ [weakSelf togglePanel]; };
    self.ballView.quitHandler = ^{ [NSApp terminate:nil]; };
    self.ballWindow.contentView = self.ballView;
    [self.ballWindow orderFrontRegardless];
}

- (void)buildPanel {
    NSRect screen = [NSScreen mainScreen].visibleFrame;
    CGFloat width = 420;
    CGFloat height = MIN(NSHeight(screen) - 48, 760);
    NSRect frame = NSMakeRect(NSMaxX(screen) - width - 18, NSMinY(screen) + 24, width, height);
    NSWindowStyleMask panelStyle = NSWindowStyleMaskTitled |
        NSWindowStyleMaskClosable |
        NSWindowStyleMaskResizable |
        NSWindowStyleMaskNonactivatingPanel;
    self.panelWindow = [[NSPanel alloc] initWithContentRect:frame styleMask:panelStyle backing:NSBackingStoreBuffered defer:NO];
    self.panelWindow.title = @"AI Radar";
    self.panelWindow.titleVisibility = NSWindowTitleHidden;
    self.panelWindow.titlebarAppearsTransparent = YES;
    self.panelWindow.movableByWindowBackground = YES;
    self.panelWindow.level = NSFloatingWindowLevel;
    self.panelWindow.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
    self.panelWindow.releasedWhenClosed = NO;
    self.panelWindow.contentMinSize = NSMakeSize(360, 420);
    [self.panelWindow setFrameAutosaveName:@"AIRadarPanelWindow"];

    WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
    [configuration.userContentController addScriptMessageHandler:self name:@"openURL"];
    [configuration.userContentController addScriptMessageHandler:self name:@"refresh"];
    [configuration.userContentController addScriptMessageHandler:self name:@"translate"];
    self.webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    self.webView.navigationDelegate = self;
    self.webView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    [self.panelWindow.contentView addSubview:self.webView];
    [self.webView setFrame:self.panelWindow.contentView.bounds];
    [self.webView loadHTMLString:[self panelHTML] baseURL:nil];
}

- (void)togglePanel {
    if (self.panelVisible) {
        [self.panelWindow orderOut:nil];
    } else {
        [self positionPanel];
        [self.panelWindow makeKeyAndOrderFront:nil];
        [NSApp activateIgnoringOtherApps:YES];
    }
    self.panelVisible = !self.panelVisible;
    self.ballView.active = self.panelVisible;
    [self.ballView setNeedsDisplay:YES];
}

- (void)positionPanel {
    NSRect screen = [NSScreen mainScreen].visibleFrame;
    NSRect frame = self.panelWindow.frame;
    NSRect ball = self.ballWindow.frame;
    CGFloat x = NSMidX(ball) >= NSMidX(screen) ? NSMinX(ball) - NSWidth(frame) - 12 : NSMaxX(ball) + 12;
    CGFloat y = NSMidY(ball) - NSHeight(frame) / 2;
    x = MAX(NSMinX(screen) + 8, MIN(x, NSMaxX(screen) - NSWidth(frame) - 8));
    y = MAX(NSMinY(screen) + 8, MIN(y, NSMaxY(screen) - NSHeight(frame) - 8));
    [self.panelWindow setFrameOrigin:NSMakePoint(x, y)];
}

- (void)loadFeed {
    NSURLComponents *components = [NSURLComponents componentsWithString:[kAPIBase stringByAppendingString:@"/api/items"]];
    components.queryItems = @[
        [NSURLQueryItem queryItemWithName:@"range" value:@"24h"],
        [NSURLQueryItem queryItemWithName:@"region" value:@"all"],
        [NSURLQueryItem queryItemWithName:@"type" value:@"all"],
        [NSURLQueryItem queryItemWithName:@"limit" value:@"760"]
    ];
    NSURLSessionDataTask *task = [[NSURLSession sharedSession] dataTaskWithURL:components.URL completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (error || !data || ![response isKindOfClass:NSHTTPURLResponse.class] || [(NSHTTPURLResponse *)response statusCode] != 200) {
                [self sendStatus:@"服务未连接" error:YES];
                [self scheduleTunnelRetry];
                return;
            }
            self.tunnelRetryCount = 0;
            NSError *jsonError = nil;
            NSDictionary *payload = [NSJSONSerialization JSONObjectWithData:data options:0 error:&jsonError];
            if (jsonError || ![payload isKindOfClass:NSDictionary.class]) {
                [self sendStatus:@"数据格式异常" error:YES];
                return;
            }
            NSArray *items = payload[@"items"] ?: @[];
            NSData *json = [NSJSONSerialization dataWithJSONObject:items options:0 error:&jsonError];
            if (jsonError) { [self sendStatus:@"无法读取列表" error:YES]; return; }
            NSString *jsonString = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
            NSDictionary *coverage = [payload[@"x_collection"] isKindOfClass:NSDictionary.class] ? payload[@"x_collection"] : @{};
            NSString *coverageJSON = [[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:coverage options:0 error:nil] encoding:NSUTF8StringEncoding];
            NSString *script = [NSString stringWithFormat:@"window.renderItems(%@,%@); window.setRadarStatus('最近 24 小时', %lu);", jsonString, coverageJSON, (unsigned long)items.count];
            [self evaluateOrQueue:script];
        });
    }];
    [task resume];
}

- (void)reloadContent {
    [self ensureTunnel];
    [self loadFeed];
    [self loadDiscussionFeed];
    [self loadPaperFeed];
    [self loadDailyBrief];
}

- (void)loadDiscussionFeed {
    NSURLComponents *components = [NSURLComponents componentsWithString:[kAPIBase stringByAppendingString:@"/api/items"]];
    components.queryItems = @[
        [NSURLQueryItem queryItemWithName:@"range" value:@"24h"],
        [NSURLQueryItem queryItemWithName:@"region" value:@"all"],
        [NSURLQueryItem queryItemWithName:@"type" value:@"discussion"],
        [NSURLQueryItem queryItemWithName:@"limit" value:@"70"]
    ];
    [[[NSURLSession sharedSession] dataTaskWithURL:components.URL completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        NSInteger statusCode = [response isKindOfClass:NSHTTPURLResponse.class] ? [(NSHTTPURLResponse *)response statusCode] : 0;
        if (error || !data || statusCode != 200) return;
        id payload = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
        if (![payload isKindOfClass:NSDictionary.class]) return;
        NSArray *items = payload[@"items"] ?: @[];
        NSData *json = [NSJSONSerialization dataWithJSONObject:items options:0 error:nil];
        if (!json) return;
        NSString *jsonString = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
        NSString *script = [NSString stringWithFormat:@"window.renderDiscussionItems(%@);", jsonString];
        dispatch_async(dispatch_get_main_queue(), ^{ [self evaluateOrQueue:script]; });
    }] resume];
}

- (void)loadPaperFeed {
    NSURLComponents *components = [NSURLComponents componentsWithString:[kAPIBase stringByAppendingString:@"/api/items"]];
    components.queryItems = @[
        [NSURLQueryItem queryItemWithName:@"range" value:@"7d"],
        [NSURLQueryItem queryItemWithName:@"region" value:@"all"],
        [NSURLQueryItem queryItemWithName:@"type" value:@"paper"],
        [NSURLQueryItem queryItemWithName:@"limit" value:@"70"]
    ];
    [[[NSURLSession sharedSession] dataTaskWithURL:components.URL completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        NSInteger statusCode = [response isKindOfClass:NSHTTPURLResponse.class] ? [(NSHTTPURLResponse *)response statusCode] : 0;
        if (error || !data || statusCode != 200) return;
        id payload = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
        if (![payload isKindOfClass:NSDictionary.class]) return;
        NSArray *items = payload[@"items"] ?: @[];
        NSData *json = [NSJSONSerialization dataWithJSONObject:items options:0 error:nil];
        if (!json) return;
        NSString *jsonString = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
        NSString *script = [NSString stringWithFormat:@"window.renderPaperItems(%@);", jsonString];
        dispatch_async(dispatch_get_main_queue(), ^{ [self evaluateOrQueue:script]; });
    }] resume];
}

- (void)loadDailyBrief {
    NSURL *url = [NSURL URLWithString:[kAPIBase stringByAppendingString:@"/api/daily-summary"]];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    request.timeoutInterval = 90;
    [[[NSURLSession sharedSession] dataTaskWithRequest:request completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        NSInteger statusCode = [response isKindOfClass:NSHTTPURLResponse.class] ? [(NSHTTPURLResponse *)response statusCode] : 0;
        NSString *script = @"window.renderDailyBrief({error:true});";
        if (!error && data && statusCode == 200) {
            id payload = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
            if ([payload isKindOfClass:NSDictionary.class]) {
                NSData *json = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
                if (json) {
                    NSString *jsonString = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
                    script = [NSString stringWithFormat:@"window.renderDailyBrief(%@);", jsonString];
                }
            }
        }
        dispatch_async(dispatch_get_main_queue(), ^{ [self evaluateOrQueue:script]; });
    }] resume];
}

- (void)sendStatus:(NSString *)status error:(BOOL)isError {
    NSString *escaped = [status stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
    NSString *script = [NSString stringWithFormat:@"window.setRadarStatus('%@', null, %s);", escaped, isError ? "true" : "false"];
    [self evaluateOrQueue:script];
}

- (void)evaluateOrQueue:(NSString *)script {
    if (!self.webViewReady) {
        [self.pendingScripts addObject:script];
        return;
    }
    [self.webView evaluateJavaScript:script completionHandler:^(id result, NSError *error) {
        if (error) NSLog(@"AI Radar JavaScript update failed: %@", error);
    }];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    self.webViewReady = YES;
    NSArray<NSString *> *scripts = [self.pendingScripts copy];
    [self.pendingScripts removeAllObjects];
    for (NSString *script in scripts) {
        [self.webView evaluateJavaScript:script completionHandler:nil];
    }
}

- (void)userContentController:(WKUserContentController *)userContentController didReceiveScriptMessage:(WKScriptMessage *)message {
    if ([message.name isEqualToString:@"refresh"]) {
        [self reloadContent];
    } else if ([message.name isEqualToString:@"openURL"] && [message.body isKindOfClass:NSString.class]) {
        NSURL *url = [NSURL URLWithString:message.body];
        if (url) [[NSWorkspace sharedWorkspace] openURL:url];
    } else if ([message.name isEqualToString:@"translate"] && [message.body isKindOfClass:NSDictionary.class]) {
        NSDictionary *body = message.body;
        NSNumber *itemID = body[@"id"];
        NSString *text = body[@"text"];
        if (itemID && [text isKindOfClass:NSString.class]) [self translateText:text forItem:itemID];
    }
}

- (void)translateText:(NSString *)text forItem:(NSNumber *)itemID {
    NSLog(@"AI Radar translation requested for item %@", itemID);
    NSString *limited = text.length > 4000 ? [text substringToIndex:4000] : text;
    NSURL *url = [NSURL URLWithString:[kAPIBase stringByAppendingString:@"/api/translate"]];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    request.HTTPMethod = @"POST";
    request.timeoutInterval = 50;
    [request setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
    request.HTTPBody = [NSJSONSerialization dataWithJSONObject:@{ @"text": limited } options:0 error:nil];
    [[[NSURLSession sharedSession] dataTaskWithRequest:request completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        NSString *translated = nil;
        NSInteger statusCode = [response isKindOfClass:NSHTTPURLResponse.class] ? [(NSHTTPURLResponse *)response statusCode] : 0;
        if (!error && data && statusCode == 200) {
            id payload = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
            if ([payload isKindOfClass:NSDictionary.class]) translated = payload[@"translation"];
        }
        NSLog(@"AI Radar translation response for item %@: status=%ld error=%@ hasText=%@", itemID, (long)statusCode, error, translated ? @"yes" : @"no");
        dispatch_async(dispatch_get_main_queue(), ^{
            NSString *script;
            if (translated) {
                NSData *json = [NSJSONSerialization dataWithJSONObject:@{ @"id": itemID, @"text": translated } options:0 error:nil];
                NSString *payloadJSON = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
                script = [NSString stringWithFormat:@"window.showTranslation(%@);", payloadJSON];
            } else {
                script = [NSString stringWithFormat:@"window.showTranslation({id:%@,error:true});", itemID];
            }
            [self evaluateOrQueue:script];
        });
    }] resume];
}

- (void)webView:(WKWebView *)webView decidePolicyForNavigationAction:(WKNavigationAction *)navigationAction decisionHandler:(void (^)(WKNavigationActionPolicy))decisionHandler {
    if (navigationAction.navigationType == WKNavigationTypeLinkActivated) {
        if (navigationAction.request.URL) [[NSWorkspace sharedWorkspace] openURL:navigationAction.request.URL];
        decisionHandler(WKNavigationActionPolicyCancel);
    } else {
        decisionHandler(WKNavigationActionPolicyAllow);
    }
}

- (NSString *)panelHTML {
    return @"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><style>"
    "*{box-sizing:border-box}body{margin:0;padding:18px 12px 14px;background:rgba(244,247,245,.94);color:#17201d;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}"
    ".head{display:flex;align-items:center;justify-content:space-between;padding:0 6px 12px}.brand{font-size:20px;font-weight:700}.sub{margin-top:4px;color:#69756f;font-size:11px}.actions{display:flex;align-items:center;gap:10px}.status{color:#69756f;font-size:10px}.refresh{border:0;background:transparent;color:#15624f;font-size:18px;cursor:pointer;padding:2px}"
    ".brief{margin:0 0 12px;padding:12px;border:1px solid #bad1c7;border-left:3px solid #28765c;border-radius:8px;background:linear-gradient(135deg,rgba(255,255,255,.94),rgba(230,243,236,.92))}.brief.loading{border-left-color:#c47a2d}.brief.failed{border-left-color:#a43f3f}.briefTop{display:flex;align-items:center;justify-content:space-between;gap:10px}.briefLabel{color:#1d6d54;font-size:10px;font-weight:700}.briefMeta{color:#75827c;font-size:9px}.briefTitle{margin-top:7px;font-size:14px;font-weight:700;line-height:1.4}.briefOverview{margin-top:6px;color:#4e5f58;font-size:11px;line-height:1.55}.briefThemes{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}.briefTheme{padding:3px 7px;border:1px solid #c6d9d0;border-radius:10px;color:#246e56;background:rgba(255,255,255,.7);font-size:9px}.briefToggle{margin-top:8px;padding:0;border:0;background:transparent;color:#17674f;font-size:9px;cursor:pointer}.briefMore{margin-top:9px;padding-top:8px;border-top:1px solid #d2dfda;color:#53625c;font-size:10px;line-height:1.5}.briefMore strong{display:block;margin:6px 0 3px;color:#2c453c;font-size:10px}.briefMore strong:first-child{margin-top:0}.briefMore ul{margin:0;padding-left:16px}.briefMore li+li{margin-top:3px}"
    ".filters{display:flex;gap:6px;overflow-x:auto;padding:0 2px 12px}.filter{border:1px solid #d3dfd8;border-radius:14px;background:rgba(255,255,255,.58);color:#627069;font-size:11px;padding:5px 10px;cursor:pointer;white-space:nowrap}.filter.active{background:#1b6b55;border-color:#1b6b55;color:#fff}.list{display:flex;flex-direction:column;gap:8px}.item{display:block;color:inherit;padding:12px;border:1px solid #dce4df;border-radius:8px;background:rgba(255,255,255,.82);cursor:pointer}.item:hover{border-color:#78a896;background:#fff}.meta{color:#77827d;font-size:10px;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.title{font-size:14px;font-weight:600;line-height:1.42}.summary{margin-top:6px;color:#59655f;font-size:11px;line-height:1.48;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.translation{margin-top:8px;border-left:2px solid #6fa58e;padding:7px 9px;color:#2d5548;background:#edf5f0;font-size:11px;line-height:1.5}.foot{margin-top:9px;color:#71807a;font-size:10px;display:flex;align-items:center;justify-content:space-between}.tag{color:#26755d}.tools{display:flex;align-items:center;gap:10px}.translate{border:0;background:transparent;color:#17674f;font-size:10px;padding:0;cursor:pointer}.empty{text-align:center;color:#69756f;font-size:12px;padding:48px 12px}.error{color:#a43f3f}</style></head><body>"
    "<div class='head'><div><div class='brand'>AI Radar</div><div class='sub'>重点账号与强相关 AI</div></div><div class='actions'><span class='status' id='status'>正在连接</span><button class='refresh' title='重新读取已采集内容' onclick=\"window.webkit.messageHandlers.refresh.postMessage('refresh')\">↻</button></div></div>"
    "<div class='brief loading' id='brief'><div class='briefTop'><span class='briefLabel'>今日 X 情报</span><span class='briefMeta' id='briefMeta'>DeepSeek 分析中</span></div><div class='briefTitle' id='briefTitle'>正在提炼今天的主要方向</div><div class='briefOverview' id='briefOverview'>只总结 AI Radar 最近 24 小时已经采集的内容。</div><div class='briefThemes' id='briefThemes'></div><button class='briefToggle' id='briefToggle' hidden onclick='toggleBrief()'>查看行动建议</button><div class='briefMore' id='briefMore' hidden></div></div>"
    "<div class='filters'><button class='filter active' data-filter='all' onclick=\"setFilter('all')\">全部</button><button class='filter' data-filter='project' onclick=\"setFilter('project')\">项目</button><button class='filter' data-filter='model' onclick=\"setFilter('model')\">模型</button><button class='filter' data-filter='paper' onclick=\"setFilter('paper')\">论文</button><button class='filter' data-filter='news' onclick=\"setFilter('news')\">资讯</button><button class='filter' data-filter='discussion' onclick=\"setFilter('discussion')\">讨论</button><button class='filter' data-filter='priority' onclick=\"setFilter('priority')\">重点账号</button></div>"
    "<div class='list' id='list'><div class='empty'>正在读取已采集内容</div></div>"
    "<script>function esc(s){return String(s||'').replace(/[&<>\\\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;',\"'\":'&#39;'}[c]})}"
    "function date(s){try{return new Date(s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}catch(e){return ''}}var allItems=[];var discussionItems=[];var paperItems=[];var activeFilter='all';var translations={};var recentCount=null;var statusError=false;"
    "var xCoverage={};function updateViewStatus(){var el=document.getElementById('status');if(statusError)return;var xCount=allItems.filter(function(i){return i.source_key==='x-ai'}).length;var target=xCoverage.target||30;var gap=xCount<target?(xCoverage.daily_read_remaining<10?' · 采集额度不足':' · 待补采'):'';el.title='最近24小时 X 筛选去重后 '+xCount+' 条，目标 '+target+' 条。每日付费读取剩余 '+(xCoverage.daily_read_remaining==null?'未知':xCoverage.daily_read_remaining)+' 条；读取数量不等于展示数量。';if(activeFilter==='discussion')el.textContent='近 24 小时高热观点 · '+discussionItems.filter(matches).length+'条';else if(activeFilter==='paper')el.textContent='近 7 天社区热门论文 · '+paperItems.filter(matches).length+'条';else if(recentCount!==null)el.textContent='24 小时 · X '+xCount+'条 · 共 '+recentCount+'条'+gap}"
    "function setFilter(filter){activeFilter=filter;document.querySelectorAll('.filter').forEach(function(b){b.classList.toggle('active',b.dataset.filter===filter)});renderFiltered();updateViewStatus()}"
    "function compact(n){try{return new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0}).format(Number(n||0))}catch(e){return String(n||0)}}"
    "function metricSummary(i){var m=i.source_key==='x-ai'?i.metrics:(i.content_type==='paper'?i.paper_metrics:null);if(!m)return'';var p=[];if(i.source_key==='x-ai'){if(m.impression_count)p.push(compact(m.impression_count)+' 浏览');if(m.like_count)p.push(compact(m.like_count)+' 赞');if(m.retweet_count)p.push(compact(m.retweet_count)+' 转发');if(m.bookmark_count)p.push(compact(m.bookmark_count)+' 收藏')}else{if(m.upvotes)p.push(compact(m.upvotes)+' 赞同');if(m.comments)p.push(compact(m.comments)+' 讨论');if(m.github_stars)p.push(compact(m.github_stars)+' GitHub stars')}return p.join(' · ')}"
    "function toggleBrief(){var more=document.getElementById('briefMore');var button=document.getElementById('briefToggle');more.hidden=!more.hidden;button.textContent=more.hidden?'查看行动建议':'收起行动建议'}"
    "function renderDailyBrief(data){var box=document.getElementById('brief');var title=document.getElementById('briefTitle');var overview=document.getElementById('briefOverview');var themes=document.getElementById('briefThemes');var more=document.getElementById('briefMore');var toggle=document.getElementById('briefToggle');box.classList.remove('loading','failed');if(data.error){box.classList.add('failed');title.textContent='今日总结暂时不可用';overview.textContent='已采集的信息仍可正常查看，稍后点击右上角刷新重试。';themes.innerHTML='';more.hidden=true;toggle.hidden=true;document.getElementById('briefMeta').textContent='DeepSeek';return}title.textContent=data.headline||'今日 AI 情报';overview.textContent=data.overview||'';themes.innerHTML=(data.themes||[]).map(function(t){return '<span class=\"briefTheme\" title=\"'+esc(t.summary)+'\">'+esc(t.name)+'</span>'}).join('');var signals=(data.key_signals||[]).map(function(x){return '<li>'+esc(x)+'</li>'}).join('');var ideas=(data.content_ideas||[]).map(function(x){return '<li>'+esc(x)+'</li>'}).join('');more.innerHTML=(signals?'<strong>值得追踪</strong><ul>'+signals+'</ul>':'')+(ideas?'<strong>学习与内容切入点</strong><ul>'+ideas+'</ul>':'');toggle.hidden=!(signals||ideas);more.hidden=true;document.getElementById('briefMeta').textContent='DeepSeek · '+(data.item_count||0)+'条 X · '+date(data.generated_at)+(data.stale?' · 上次结果':'')}"
    "function matches(i){if(activeFilter==='priority')return(i.tags||[]).indexOf('重点账号')>=0;if(activeFilter==='all')return true;if(activeFilter==='discussion')return i.content_type==='discussion'&&(i.source_key==='x-ai'||Number(i.engagement||0)>=500);if(activeFilter==='paper')return i.content_type==='paper'&&i.model_relevant;return i.content_type===activeFilter}"
    "function renderItems(items,coverage){allItems=items||[];xCoverage=coverage||{};renderFiltered()}"
    "function renderDiscussionItems(items){discussionItems=items||[];if(activeFilter==='discussion')renderFiltered();updateViewStatus()}"
    "function renderPaperItems(items){paperItems=items||[];if(activeFilter==='paper')renderFiltered();updateViewStatus()}"
    "function openItem(event,node){if(event.target.closest('button'))return;window.webkit.messageHandlers.openURL.postMessage(node.dataset.url)}"
    "function requestTranslation(event,id){event.preventDefault();event.stopPropagation();var node=document.querySelector('[data-id=\"'+id+'\"] .translation');var item=allItems.concat(discussionItems,paperItems).find(function(x){return x.id===id});node.hidden=false;node.textContent='DeepSeek 翻译中…';if(item)window.webkit.messageHandlers.translate.postMessage({id:id,text:item.summary||item.title||''})}"
    "function renderFiltered(){var list=document.getElementById('list');var sourceItems=activeFilter==='discussion'?discussionItems:(activeFilter==='paper'?paperItems:allItems);var items=sourceItems.filter(matches);if(!items.length){list.innerHTML='<div class=\"empty\">'+(activeFilter==='paper'?'近 7 天暂无模型强相关论文':'当前分类暂无高热内容')+'</div>';return}list.innerHTML=items.map(function(i){var source=(i.tags||[]).indexOf('官方发布')>=0?'X 官方发布':i.author||((i.sources&&i.sources[0]&&i.sources[0].name)||'AI Radar');var region=i.region==='china'?'国内':'海外';var tag=(i.tags||[]).indexOf('重点账号')>=0?'重点账号':((i.tags||[]).indexOf('社区热门')>=0?'社区热门':((i.tags||[]).indexOf('高热度')>=0?'高热度':'模型强相关'));var english=(i.tags||[]).indexOf('en')>=0;var translation=english?'<button class=\"translate\" onclick=\"requestTranslation(event,'+i.id+')\">翻译</button>':'';var cached=translations[i.id];var translated=cached?'<div class=\"translation\">'+esc(cached)+'</div>':'<div class=\"translation\" hidden></div>';var metrics=metricSummary(i);if(i.source_key==='x-ai'&&i.metrics_fetched_at)metrics+=' · 数据 '+date(i.metrics_fetched_at);return '<div class=\"item\" data-id=\"'+i.id+'\" data-url=\"'+esc(i.url)+'\" role=\"link\" tabindex=\"0\" onclick=\"openItem(event,this)\"><div class=\"meta\">'+esc(source)+' · '+region+' · '+date(i.effective_at)+'</div><div class=\"title\">'+esc(i.title)+'</div>'+(i.summary?'<div class=\"summary\">'+esc(i.summary)+'</div>':'')+translated+'<div class=\"foot\"><span class=\"tag\">'+tag+'</span><span class=\"tools\">'+translation+'<span>'+esc(metrics||(i.engagement?'热度 '+i.engagement:'AI'))+'</span></span></div></div>'}).join('')}"
    "function showTranslation(data){var value=data.error?'翻译暂时失败，请稍后重试':data.text;translations[data.id]=value;var node=document.querySelector('[data-id=\"'+data.id+'\"] .translation');if(!node)return;node.hidden=false;node.textContent=value}"
    "function setRadarStatus(text,count,error){var el=document.getElementById('status');statusError=!!error;if(count!==null)recentCount=count;el.textContent=count===null?text:(text+' · '+count+'条');el.className='status'+(error?' error':'');if(!error)updateViewStatus()}"
    "</script></body></html>";
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        AppDelegate *delegate = [[AppDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return EXIT_SUCCESS;
}
