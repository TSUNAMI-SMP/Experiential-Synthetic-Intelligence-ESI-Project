package org.esi.taskui;
import java.awt.*;
import java.lang.instrument.Instrumentation;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import javax.swing.*;
import javax.swing.text.JTextComponent;
import com.sun.net.httpserver.HttpServer;

/** UI automation for the task-owned editor. Uses only public AWT/Swing controls.
 * No application internals, bytecode transformers, model/scene file writers or
 * license checks are accessed or changed. Bound exclusively to loopback. */
public class UiAgent {
 static IdentityHashMap<Component,Integer> identifiers = new IdentityHashMap<>();
 static Map<Integer,Component> controls = new HashMap<>();
 static int next = 1;
 static int id(Component c) {
  Integer n = identifiers.get(c);
  if(n==null) { n=next++; identifiers.put(c,n); controls.put(n,c); }
  return n;
 }
 static String quote(String s) {
  StringBuilder b=new StringBuilder("\"");
  if(s!=null) for(char c:s.toCharArray()) {
   switch(c) {case '"':b.append("\\\"");break;case '\\':b.append("\\\\");break;
    case '\n':b.append("\\n");break;case '\r':b.append("\\r");break;
    case '\t':b.append("\\t");break;default:if(c<32)b.append(String.format("\\u%04x",(int)c));else b.append(c);
   }
  }
  return b.append('"').toString();
 }
 static void visit(Component c,int parent, java.util.List<String> rows, Set<Component> seen) {
  if(!seen.add(c)||rows.size()>20000)return;
  String text="";
  if(c instanceof AbstractButton) text=((AbstractButton)c).getText();
  else if(c instanceof JLabel)text=((JLabel)c).getText();
  else if(c instanceof JPasswordField)text="[password]";
  else if(c instanceof JTextComponent)text=((JTextComponent)c).getText();
  else if(c instanceof Frame)text=((Frame)c).getTitle();
  else if(c instanceof Dialog)text=((Dialog)c).getTitle();
  else if(c instanceof JComboBox)text=String.valueOf(((JComboBox<?>)c).getSelectedItem());
  Rectangle bounds=c.getBounds();
  if(c.isShowing())try {Point p=c.getLocationOnScreen();bounds.x=p.x;bounds.y=p.y;}catch(Exception e){}
  int n=id(c);
  String row="{\"id\":"+n+",\"parent\":"+parent+",\"class\":"+quote(c.getClass().getName())+
   ",\"text\":"+quote(text)+",\"name\":"+quote(c.getName())+",\"showing\":"+c.isShowing()+
   ",\"enabled\":"+c.isEnabled()+",\"x\":"+bounds.x+",\"y\":"+bounds.y+
   ",\"w\":"+bounds.width+",\"h\":"+bounds.height;
  if(c instanceof JComponent)row+=",\"tooltip\":"+quote(((JComponent)c).getToolTipText());
  if(c instanceof AbstractButton)row+=",\"selected\":"+((AbstractButton)c).isSelected();
  if(c instanceof JComboBox) {
   JComboBox<?> combo=(JComboBox<?>)c;
   StringJoiner items=new StringJoiner(",","[","]");
   for(int i=0;i<Math.min(combo.getItemCount(),500);i++)items.add(quote(String.valueOf(combo.getItemAt(i))));
   row+=",\"items\":"+items;
  }
  if(c instanceof JTable) {
   JTable t=(JTable)c;
   StringJoiner data=new StringJoiner(",","[","]");
   for(int r=0;r<Math.min(t.getRowCount(),500);r++) {
    StringJoiner values=new StringJoiner(",","[","]");
    for(int col=0;col<t.getColumnCount();col++)values.add(quote(String.valueOf(t.getValueAt(r,col))));
    data.add(values.toString());
   }
   row+=",\"cells\":"+data+",\"row_height\":"+t.getRowHeight();
   StringJoiner selection=new StringJoiner(",","[","]");
   for(int selected:t.getSelectedRows())selection.add(String.valueOf(selected));
   row+=",\"selected_rows\":"+selection;
  }
  rows.add(row+"}");
  if(c instanceof JMenu)for(Component child:((JMenu)c).getMenuComponents())visit(child,n,rows,seen);
  if(c instanceof Container)for(Component child:((Container)c).getComponents())visit(child,n,rows,seen);
 }
 static String snapshot() {
  java.util.List<String> rows=new ArrayList<>();
  Set<Component> seen=Collections.newSetFromMap(new IdentityHashMap<>());
  for(Window w:Window.getWindows())if(w.isShowing())visit(w,0,rows,seen);
  return "["+String.join(",",rows)+"]";
 }
 static Map<String,String> parse(String query) {
  Map<String,String> data=new HashMap<>();
  for(String part:query.split("&")) {
   String[] kv=part.split("=",2);
   if(kv.length==2)data.put(URLDecoder.decode(kv[0],StandardCharsets.UTF_8),URLDecoder.decode(kv[1],StandardCharsets.UTF_8));
  }
  return data;
 }
 static String threadSnapshot() {
  StringJoiner rows=new StringJoiner(",","[","]");
  for(Map.Entry<Thread,StackTraceElement[]> entry:Thread.getAllStackTraces().entrySet()) {
   Thread thread=entry.getKey();
   StringJoiner stack=new StringJoiner(",","[","]");
   StackTraceElement[] frames=entry.getValue();
   for(int i=0;i<Math.min(frames.length,32);i++)stack.add(quote(frames[i].toString()));
   rows.add("{\"name\":"+quote(thread.getName())+",\"state\":"+quote(thread.getState().toString())+",\"stack\":"+stack+"}");
  }
  return rows.toString();
 }
 static void action(Map<String,String> data) {
  Component c=controls.get(Integer.parseInt(data.get("id")));
  if(c==null||!c.isEnabled())throw new IllegalArgumentException("Control missing or disabled");
  if(!c.isShowing()&&!(c instanceof JMenuItem))throw new IllegalArgumentException("Control not visible");
  String act=data.get("act"),value=data.getOrDefault("value","");
  switch(act) {
   case "click":
    if(!(c instanceof AbstractButton))throw new IllegalArgumentException("Not a button");
    ((AbstractButton)c).doClick();break;
   case "text":
    if(!(c instanceof JTextComponent))throw new IllegalArgumentException("Not an input field");
    c.requestFocusInWindow();((JTextComponent)c).setText(value);
    if(c instanceof JTextField)((JTextField)c).postActionEvent();break;
   case "select":
    if(!(c instanceof JComboBox))throw new IllegalArgumentException("Not a selector");
    JComboBox<?> cb=(JComboBox<?>)c;
    boolean found=false;
    for(int i=0;i<cb.getItemCount();i++)if(String.valueOf(cb.getItemAt(i)).equals(value)){cb.setSelectedIndex(i);found=true;break;}
    if(!found)throw new IllegalArgumentException("Option missing");break;
   case "cell":
    if(!(c instanceof JTable))throw new IllegalArgumentException("Not a table");
    JTable t=(JTable)c;
    int r=Integer.parseInt(data.get("row")),col=Integer.parseInt(data.get("column"));
    if(!t.isCellEditable(r,col))throw new IllegalArgumentException("Cell not editable");
    if(t.isEditing())t.getCellEditor().stopCellEditing();
    t.getModel().setValueAt(value,r,col);break;
   default:throw new IllegalArgumentException("Unknown UI operation");
  }
 }
 public static void premain(String args,Instrumentation instrumentation) throws Exception {
  HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",8769),0);
  server.setExecutor(Executors.newFixedThreadPool(4,r->{Thread t=new Thread(r,"Editor-UI-control");t.setDaemon(true);return t;}));
  server.createContext("/tree",exchange->{
   try {
    if(!exchange.getRequestMethod().equals("GET"))throw new IllegalArgumentException("GET only");
    FutureTask<String> task=new FutureTask<>(()->snapshot());
    SwingUtilities.invokeLater(task);
    String result;
    try {result=task.get(5,TimeUnit.SECONDS);}catch(TimeoutException e){task.cancel(false);throw e;}
    byte[] bytes=result.getBytes(StandardCharsets.UTF_8);
    exchange.getResponseHeaders().set("Content-Type","application/json; charset=utf-8");
    exchange.sendResponseHeaders(200,bytes.length);exchange.getResponseBody().write(bytes);
   }catch(Exception e){byte[] b=e.toString().getBytes(StandardCharsets.UTF_8);exchange.sendResponseHeaders(400,b.length);exchange.getResponseBody().write(b);}
   finally {exchange.close();}
  });
  server.createContext("/threads",exchange->{
   try {
    if(!exchange.getRequestMethod().equals("GET"))throw new IllegalArgumentException("GET only");
    byte[] bytes=threadSnapshot().getBytes(StandardCharsets.UTF_8);
    exchange.getResponseHeaders().set("Content-Type","application/json; charset=utf-8");
    exchange.sendResponseHeaders(200,bytes.length);exchange.getResponseBody().write(bytes);
   }catch(Exception e){byte[] b=e.toString().getBytes(StandardCharsets.UTF_8);exchange.sendResponseHeaders(400,b.length);exchange.getResponseBody().write(b);}
   finally {exchange.close();}
  });
  server.createContext("/action",exchange->{
   try {
    if(!exchange.getRequestMethod().equals("POST"))throw new IllegalArgumentException("POST only");
    byte[] request=exchange.getRequestBody().readNBytes(8193);
    if(request.length>8192)throw new IllegalArgumentException("Request too large");
    Map<String,String> data=parse(new String(request,StandardCharsets.UTF_8));
    SwingUtilities.invokeLater(()->{try {action(data);}catch(Exception e){System.err.println("UI control: "+e);}});
    byte[] bytes="{\"queued\":true}".getBytes(StandardCharsets.UTF_8);
    exchange.sendResponseHeaders(202,bytes.length);exchange.getResponseBody().write(bytes);
   }catch(Exception e){byte[] b=e.toString().getBytes(StandardCharsets.UTF_8);exchange.sendResponseHeaders(400,b.length);exchange.getResponseBody().write(b);}
   finally {exchange.close();}
  });
  server.start();System.out.println("Task UI automation active at loopback port 8769");
 }
}
