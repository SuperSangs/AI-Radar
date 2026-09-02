#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

static NSString * const kAPIBase = @"http://127.0.0.1:8765";

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
@property(nonatomic, copy) NSString *pendingScript;
@property(nonatomic) BOOL webViewReady;
@property(nonatomic) BOOL panelVisible;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    [self buildBall];
    [self buildPanel];
    [self loadFeed];
    self.timer = [NSTimer scheduledTimerWithTimeInterval:60 target:self selector:@selector(loadFeed) userInfo:nil repeats:YES];
}

- (void)applicationWillTerminate:(NSNotification *)notification { [self.timer invalidate]; }

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
        [NSURLQueryItem queryItemWithName:@"limit" value:@"740"]
    ];
    NSURLSessionDataTask *task = [[NSURLSession sharedSession] dataTaskWithURL:components.URL completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (error || !data || ![response isKindOfClass:NSHTTPURLResponse.class] || [(NSHTTPURLResponse *)response statusCode] != 200) {
                [self sendStatus:@"服务未连接" error:YES];
                return;
            }
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
            NSString *script = [NSString stringWithFormat:@"window.renderItems(%@); window.setRadarStatus('最近 24 小时', %lu);", jsonString, (unsigned long)items.count];
            [self evaluateOrQueue:script];
        });
    }];
    [task resume];
}

- (void)sendStatus:(NSString *)status error:(BOOL)isError {
    NSString *escaped = [status stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
    NSString *script = [NSString stringWithFormat:@"window.setRadarStatus('%@', null, %s);", escaped, isError ? "true" : "false"];
    [self evaluateOrQueue:script];
}

- (void)evaluateOrQueue:(NSString *)script {
    if (!self.webViewReady) {
        self.pendingScript = script;
        return;
    }
    [self.webView evaluateJavaScript:script completionHandler:^(id result, NSError *error) {
        if (error) NSLog(@"AI Radar JavaScript update failed: %@", error);
    }];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    self.webViewReady = YES;
    if (self.pendingScript.length > 0) {
        NSString *script = self.pendingScript;
        self.pendingScript = nil;
        [self.webView evaluateJavaScript:script completionHandler:nil];
    }
}

- (void)userContentController:(WKUserContentController *)userContentController didReceiveScriptMessage:(WKScriptMessage *)message {
    if ([message.name isEqualToString:@"refresh"]) {
        [self loadFeed];
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
    ".head{display:flex;align-items:center;justify-content:space-between;padding:0 6px 12px}.brand{font-size:20px;font-weight:700}.sub{margin-top:4px;color:#69756f;font-size:11px}.actions{display:flex;align-items:center;gap:10px}.status{color:#69756f;font-size:10px}.refresh{border:0;background:transparent;color:#15624f;font-size:18px;cursor:pointer;padding:2px}.filters{display:flex;gap:6px;overflow-x:auto;padding:0 2px 12px}.filter{border:1px solid #d3dfd8;border-radius:14px;background:rgba(255,255,255,.58);color:#627069;font-size:11px;padding:5px 10px;cursor:pointer;white-space:nowrap}.filter.active{background:#1b6b55;border-color:#1b6b55;color:#fff}.list{display:flex;flex-direction:column;gap:8px}.item{display:block;color:inherit;padding:12px;border:1px solid #dce4df;border-radius:8px;background:rgba(255,255,255,.82);cursor:pointer}.item:hover{border-color:#78a896;background:#fff}.meta{color:#77827d;font-size:10px;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.title{font-size:14px;font-weight:600;line-height:1.42}.summary{margin-top:6px;color:#59655f;font-size:11px;line-height:1.48;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.translation{margin-top:8px;border-left:2px solid #6fa58e;padding:7px 9px;color:#2d5548;background:#edf5f0;font-size:11px;line-height:1.5}.foot{margin-top:9px;color:#71807a;font-size:10px;display:flex;align-items:center;justify-content:space-between}.tag{color:#26755d}.tools{display:flex;align-items:center;gap:10px}.translate{border:0;background:transparent;color:#17674f;font-size:10px;padding:0;cursor:pointer}.empty{text-align:center;color:#69756f;font-size:12px;padding:48px 12px}.error{color:#a43f3f}</style></head><body>"
    "<div class='head'><div><div class='brand'>AI Radar</div><div class='sub'>重点账号与强相关 AI</div></div><div class='actions'><span class='status' id='status'>正在连接</span><button class='refresh' title='重新读取已采集内容' onclick=\"window.webkit.messageHandlers.refresh.postMessage('refresh')\">↻</button></div></div>"
    "<div class='filters'><button class='filter active' data-filter='all' onclick=\"setFilter('all')\">全部</button><button class='filter' data-filter='project' onclick=\"setFilter('project')\">项目</button><button class='filter' data-filter='model' onclick=\"setFilter('model')\">模型</button><button class='filter' data-filter='paper' onclick=\"setFilter('paper')\">论文</button><button class='filter' data-filter='news' onclick=\"setFilter('news')\">资讯</button><button class='filter' data-filter='discussion' onclick=\"setFilter('discussion')\">讨论</button><button class='filter' data-filter='priority' onclick=\"setFilter('priority')\">重点账号</button></div>"
    "<div class='list' id='list'><div class='empty'>正在读取已采集内容</div></div>"
    "<script>function esc(s){return String(s||'').replace(/[&<>\\\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;',\"'\":'&#39;'}[c]})}"
    "function date(s){try{return new Date(s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}catch(e){return ''}}var allItems=[];var activeFilter='all';var translations={};"
    "function setFilter(filter){activeFilter=filter;document.querySelectorAll('.filter').forEach(function(b){b.classList.toggle('active',b.dataset.filter===filter)});renderFiltered()}"
    "function matches(i){if(activeFilter==='priority')return(i.tags||[]).indexOf('重点账号')>=0;if(activeFilter==='all')return true;return i.content_type===activeFilter}"
    "function renderItems(items){allItems=items||[];renderFiltered()}"
    "function openItem(event,node){if(event.target.closest('button'))return;window.webkit.messageHandlers.openURL.postMessage(node.dataset.url)}"
    "function requestTranslation(event,id){event.preventDefault();event.stopPropagation();var node=document.querySelector('[data-id=\"'+id+'\"] .translation');var item=allItems.find(function(x){return x.id===id});node.hidden=false;node.textContent='DeepSeek 翻译中…';if(item)window.webkit.messageHandlers.translate.postMessage({id:id,text:item.summary||item.title||''})}"
    "function renderFiltered(){var list=document.getElementById('list');var items=allItems.filter(matches);if(!items.length){list.innerHTML='<div class=\"empty\">当前分类暂无内容</div>';return}list.innerHTML=items.map(function(i){var source=(i.sources&&i.sources[0]&&i.sources[0].name)||'AI Radar';var region=i.region==='china'?'国内':'海外';var tag=(i.tags||[]).indexOf('重点账号')>=0?'重点账号':'强相关';var english=(i.tags||[]).indexOf('en')>=0;var translation=english?'<button class=\"translate\" onclick=\"requestTranslation(event,'+i.id+')\">翻译</button>':'';var cached=translations[i.id];var translated=cached?'<div class=\"translation\">'+esc(cached)+'</div>':'<div class=\"translation\" hidden></div>';return '<div class=\"item\" data-id=\"'+i.id+'\" data-url=\"'+esc(i.url)+'\" role=\"link\" tabindex=\"0\" onclick=\"openItem(event,this)\"><div class=\"meta\">'+esc(source)+' · '+region+' · '+date(i.effective_at)+'</div><div class=\"title\">'+esc(i.title)+'</div>'+(i.summary?'<div class=\"summary\">'+esc(i.summary)+'</div>':'')+translated+'<div class=\"foot\"><span class=\"tag\">'+tag+'</span><span class=\"tools\">'+translation+'<span>'+(i.engagement?'热度 '+i.engagement:'AI')+'</span></span></div></div>'}).join('')}"
    "function showTranslation(data){var value=data.error?'翻译暂时失败，请稍后重试':data.text;translations[data.id]=value;var node=document.querySelector('[data-id=\"'+data.id+'\"] .translation');if(!node)return;node.hidden=false;node.textContent=value}"
    "function setRadarStatus(text,count,error){var el=document.getElementById('status');el.textContent=count===null?text:(text+' · '+count+'条');el.className='status'+(error?' error':'')}"
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
