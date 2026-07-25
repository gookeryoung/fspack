import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Pane {
    id: card
    property string distroName: ""
    property string version: ""
    property string status: "Stopped"   // Running / Stopped
    property string diskUsage: ""
    property string ipAddress: ""

    Layout.fillWidth: true
    Layout.preferredHeight: 150
    padding: 20

    // 卡片背景（圆角 + 1px 边框）
    background: Rectangle {
        color: Theme.isDark ? "#1E1F2A" : "#FFFFFF"
        radius: 10
        border.color: Theme.isDark ? "#2E2F3A" : "#E8E8E8"
        border.width: 1
        Behavior on color { ColorAnimation { duration: 200 } }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 12

        // 第一行：名称 + 状态徽标
        RowLayout {
            Layout.fillWidth: true
            Label {
                text: card.distroName
                font.pixelSize: 16
                font.bold: true
                color: Theme.isDark ? "#E0E0EF" : "#1A1A1A"
            }
            Label {
                text: "v" + card.version
                font.pixelSize: 11
                color: Theme.isDark ? "#7A7A8A" : "#999"
            }
            Item { Layout.fillWidth: true }

            // 状态胶囊
            Rectangle {
                radius: 10
                height: 20
                width: statusText.width + 18
                color: card.status === "Running"
                      ? "#E8F5E9" : "#F0F0F0"
                border.color: card.status === "Running"
                            ? "#4CAF50" : "#CCC"
                border.width: 1

                Label {
                    id: statusText
                    anchors.centerIn: parent
                    text: card.status
                    font.pixelSize: 10
                    color: card.status === "Running"
                          ? "#2E7D32" : "#888"
                }
            }
        }

        // 第二行：磁盘 + IP
        RowLayout {
            Layout.fillWidth: true
            spacing: 24
            Label {
                text: "💽  " + card.diskUsage
                font.pixelSize: 12
                color: Theme.isDark ? "#9A9AAA" : "#666"
            }
            Label {
                text: "🌐  " + card.ipAddress
                font.pixelSize: 12
                color: Theme.isDark ? "#9A9AAA" : "#666"
            }
        }

        Item { Layout.fillHeight: true }

        // 第三行：操作按钮
        RowLayout {
            spacing: 8
            Repeater {
                model: [
                    { label: "▶ Start",   icon: "⏵" },
                    { label: "■ Stop",    icon: "⏹" },
                    { label: "VS Code",   icon: "💻" },
                    { label: "Files",     icon: "📁" }
                ]
                delegate: Rectangle {
                    width: 72; height: 28; radius: 6
                    color: mouse.containsMouse
                          ? (Theme.isDark ? "#3A3B4A" : "#EDF3FF")
                          : (Theme.isDark ? "#2A2B3A" : "#F5F6F8")
                    border.color: Theme.isDark ? "#3A3B4A" : "#E0E0E0"
                    border.width: 1
                    MouseArea {
                        id: mouse
                        anchors.fill: parent
                        hoverEnabled: true
                    }
                    RowLayout {
                        anchors.centerIn: parent
                        spacing: 4
                        Label {
                            text: modelData.icon
                            font.pixelSize: 11
                        }
                        Label {
                            text: modelData.label
                            font.pixelSize: 11
                            color: Theme.isDark ? "#B0B0BF" : "#444"
                        }
                    }
                }
            }
        }
    }
}